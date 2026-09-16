# Survey: speech and voice recognition/purge models for privacy preservation

**Status: survey. Nothing here is built, selected, or approved.** This document evaluates the
published state of the art in speech detection, speaker diarization and self-supervised speech
representation *as candidate mechanisms for removing human voice from an autonomous acoustic
sensor network*, and states the privacy threat model that any such mechanism has to survive.
It selects no model, changes no code, adds no dependency, and grants no permission to record.

Audience: an operator who has to decide whether DAMA Hear can honestly claim "we do not keep
human speech", and who needs to know which claims in that sentence are measured, which are
vendor-reported, and which are unavailable at any price today.

The policy this serves is `docs/data-governance.md` §2, which already permits "voice/activity
classification, speech suppression, masking, deletion of excluded windows" as redaction
implementations, and already requires that "operators must treat uncertain redaction as
unredacted data". This survey is the evidence base for choosing one of those implementations.
It does not amend the policy.

---

## ⚠️WHAT THIS SURVEY DOES NOT ESTABLISH

**1. No number here was measured on this fleet.** Every latency, accuracy and footprint figure
is quoted from a vendor, a paper, or a model card, on hardware that is not ours, on audio that is
not ours. The nodes in this fleet write 5.0 s, 48 kHz, 16-bit mono WAVs outdoors
(`docs/clip-pipeline.md` §1); none of the cited benchmarks contains a single second of that
material. Treat every table below as a *shortlist filter*, not as a prediction.

**2. The most load-bearing number in the whole field does not exist.** Nobody has published a
rigorous false-positive rate for any VAD on continuous outdoor audio — wind, rain, dawn chorus,
cicada and cricket stridulation. The closest published proxy is whole-clip accuracy on ESC-50,
a curated dataset of isolated 5 s sounds, which is not a field condition. §3.4 states this
explicitly rather than borrowing a number from an adjacent benchmark.

**3. A gateway-side speech filter cannot deliver zero retention, and this survey will not
pretend otherwise.** In the pipeline as it exists today the clip is written to the node's SD
card before anything classifies it (`docs/clip-pipeline.md` §1). Any filter that runs after the
drain reduces *dissemination*, not *capture*. §7 treats this as the primary threat, not a
footnote.

**4. "Speech detected" is a model output, and the clip pipeline's existing refusal applies
unchanged.** `docs/clip-pipeline.md` already refuses to treat a tagger score as an observation.
A VAD score is the same kind of object. The difference is the asymmetry of the cost: a
false negative leaks a voice, a false positive deletes a gunshot. §7.6 states the operating
point that asymmetry forces, and it is not "maximise F1".

**5. This is not legal advice.** §6 cites articles and sections because an engineer needs to
know which design choices are load-bearing for which obligation. Whether a specific deployment
is lawful is a question for counsel and a DPIA, not for this file.

**6. Several artifacts surveyed here would be refused on licence grounds alone.** This project
has already refused BirdNET over CC BY-NC-SA weights (`docs/clip-pipeline.md`). §5.4 applies the
same test to every candidate, and three of them fail it.

---

## 0. Verification discipline

Claims carry one of four marks. Nothing is asserted without one.

| Mark | Meaning |
|---|---|
| **[P]** | Primary source: the project's own LICENSE, model card, release notes, or the paper. |
| **[V]** | Vendor benchmark: published by a party with an interest in the result. Not independent. |
| **[R]** | Peer-reviewed or challenge result from a third party. |
| **[U]** | **Unverified.** Widely repeated, not confirmed from a primary source this session. |

Verification date for every **[P]**/**[V]**/**[R]** mark below: **2026-09-15**. Sources are
indexed in §10.

A fifth category matters more than the other four: facts that **do not exist**. They are listed
in §9 as open measurements, not silently interpolated.

---

## 1. What this fleet actually has to filter

Any model evaluation that ignores the input contract is theatre. The contract:

| Property | Value | Source |
|---|---|---|
| Node MCU | ESP32-S3 + RFM95W, PSRAM-dependent pin map | `docs/esp32s3-lora-node.md` |
| Acquisition rate | 48 kHz (`fs_acquisition_hz: 48000`, verified on all three nodes) | `docs/acoustic-stack.md` §"Acquisition at 48 kHz" |
| Clip written | 5.0 s, 48 kHz, 16-bit mono WAV, 480,044 B, to node SD `/clips` | `docs/clip-pipeline.md` §1 |
| Raw PSRAM ring | ~80 s / 7.68 MB request, tiered by PSRAM size | `docs/acoustic-stack.md` |
| Existing tagger input | 48 kHz clips decimated to 32 kHz; nothing above 16 kHz reaches it | `docs/clip-pipeline.md` §"WHAT THIS PIPELINE DOES NOT ESTABLISH" |
| Retention classes | `R0-derived` 90 d, `R1-clip` 30 d, `R2-raw` 7 d, `R3-evidence` hold, `R4-audit` 1 y | `docs/data-governance.md` §4 |
| Redaction ordering rule | "before uplink and before durable storage where technically possible" | `docs/data-governance.md` §2 |
| Deletion evidence rule | resolve all replicas, "emits a signed deletion receipt" | `docs/data-governance.md` §6 |

Four consequences that decide the shortlist before any accuracy number is consulted:

1. **Every speech model in this survey wants 16 kHz mono.** pyannote segmentation-3.0 **[P]**,
   Silero **[P]**, TEN VAD **[P]**, Whisper **[P]** and every SSL encoder **[P]** are 16 kHz.
   `hear/resample.py` already decimates 48 kHz → lower rates for the tagger, so the decimation
   path exists; the 16 kHz variant is not written.
2. **The clip is 5.0 s; pyannote's window is 10 s.** `segmentation-3.0` "ingests 10 seconds of
   mono audio" **[P]**. A 5 s clip must be padded, and padding a diarizer's receptive field with
   silence is an untested operating point for its powerset head. Silero (32 ms windows) and
   WebRTC (10/20/30 ms frames) have no such mismatch.
3. **The MCU cannot run any of these.** ONNX Runtime targets glibc platforms; there is no
   aarch64-MCU wheel and no ESP32-S3 ONNX Runtime. On-node speech gating would mean ESP-DL or
   TFLite-Micro with a model this survey did not find a ready candidate for (§9, open item 4).
   Everything in §2–§5 is a **gateway** capability.
4. **The clip is already on the SD card by the time a gateway sees it.** This is the whole
   privacy argument, and §7 is about it.

---

## 2. pyannote.audio 3.1 — segmentation, diarization, overlap

### 2.1 What the architecture actually is

`pyannote/segmentation-3.0` is **not** a transformer and **not** an SSL model. It is
`PyanNet`: **SincNet → BiLSTM → feed-forward → classifier**, on raw waveform **[P]**. The
INTERSPEECH 2023 powerset paper states it uses "the exact same architecture" as the 2.1
segmentation model, and that the *only* changes are the output size 3→7, sigmoid→softmax, and
BCE→cross-entropy **[R]**. The `PyanNet` class defaults are `hidden_size=128, num_layers=2,
bidirectional=True`, `SINCNET_DEFAULTS = {"stride": 10}` **[P]**; the paper describes four
BiLSTMs. The shipped checkpoint's `config.yaml` is gated, so **which of the two the released
weights use is [U]** — and so is the parameter count, which pyannote does not publish anywhere
(§9, open item 1).

The takeaway an operator needs: the segmentation model is a small recurrent net over a learned
filterbank. Its cost is not transformer-shaped. Its *pipeline* cost is, because of what gets
bolted onto it.

### 2.2 Powerset encoding and overlap

The model emits a `(num_frames, 7)` matrix over a 10 s window, where the 7 classes are
*non-speech*, *spk1*, *spk2*, *spk3*, *spk1+2*, *spk1+3*, *spk2+3* **[P]** — i.e.
`max_speakers_per_chunk = 3`, `max_speakers_per_frame = 2` **[P]**.

Consequences:

- **Three or more simultaneous speakers are architecturally unrepresentable in a frame.** The
  paper justifies this as marginal: such frames are **1.6 %** of its compound dataset and
  **0.73 %** of DIHARD III **[R]**. For a *privacy* filter, "unrepresentable" is the wrong word
  to be comfortable with — but note the failure mode is a *mis-count*, not a miss: a 3-speaker
  frame still resolves to a speech class, not to non-speech. Overlap error degrades diarization,
  not speech detection.
- The powerset head **eliminates the detection threshold hyperparameter**, replacing it with
  `argmax` **[R]**. For a privacy gate that is a loss, not a win: §7.6 requires the ability to
  move the operating point toward recall, and `argmax` does not offer one. Recovering a knob
  means using `Powerset.to_multilabel` with the `"soft"` option added in 3.1 **[P]**, which is a
  different, less-tested path.

### 2.3 Using it as a VAD without the diarizer

The model card gives the recipe directly: `pyannote.audio.pipelines.VoiceActivityDetection(
segmentation=model)` and `OverlappedSpeechDetection(...)`, with
`{"min_duration_on": 0.0, "min_duration_off": 0.0}` **[P]**. This matters commercially and
legally: the VAD path needs only the MIT segmentation checkpoint. It does **not** need the
WeSpeaker embedder, does **not** need clustering, and does **not** build a speaker
representation — which is precisely the processing that would drag the deployment into GDPR
Art. 9 and the US biometric statutes (§6.1, §6.3). **A privacy filter should never run full
diarization.** Identifying *who* spoke to decide *that* someone spoke is the exact inversion of
data minimisation.

The card also warns the model "cannot be used to perform speaker diarization of full recordings
on its own (it only processes 10 s chunks)" **[P]**.

### 2.4 The 3.1 pipeline, for completeness

`speaker-diarization-3.1` = `segmentation-3.0` + `pyannote/wespeaker-voxceleb-resnet34-LM`
embeddings + agglomerative clustering (`method: centroid`, `threshold: 0.7045654963945799`,
`min_cluster_size: 12`), batch sizes 32/32 **[P]**. The only change from 3.0 is that it "removes
the problematic use of `onnxruntime`" — both components now run in pure PyTorch **[P]**; the
3.1.0 changelog records `BREAKING(setup): remove onnxruntime dependency` **[P]**. It is a
deployment change, not an accuracy change (§2.5 confirms this numerically).

### 2.5 Published DER — and why it is an optimistic ceiling

Model-card benchmark, fully automatic, **no forgiveness collar, overlap scored**, no per-dataset
tuning **[P]**:

| Benchmark | DER % | FA % | Miss % | Conf % |
|---|---:|---:|---:|---:|
| AISHELL-4 | 12.2 | 3.8 | 4.4 | 4.0 |
| AliMeeting (ch. 1) | 24.4 | 4.4 | 10.0 | 10.0 |
| AMI (headset mix) | 18.8 | 3.6 | 9.5 | 5.7 |
| AMI (array1 ch. 1) | 22.4 | 3.8 | 11.2 | 7.5 |
| AVA-AVD | 50.0 | 10.8 | 15.7 | 23.4 |
| DIHARD 3 (full) | 21.7 | 6.2 | 8.1 | 7.3 |
| MSDWild | 25.3 | 5.8 | 8.0 | 11.5 |
| REPERE (phase 2) | 7.8 | 1.8 | 2.6 | 3.5 |
| VoxConverse (v0.3) | 11.3 | 4.1 | 3.4 | 3.8 |

3.0's table is within noise of this (AISHELL-4 12.3, AliMeeting 24.3, AMI-IHM 19.0, DIHARD 21.7,
VoxConverse 11.3) **[P]** — confirming 3.1 was a runtime change.

**Earnings21 does not appear in any pyannote benchmark table reachable this session, and neither
does Ego4D in the 3.1 card.** Do not cite a pyannote Earnings21 DER; it was asked for and it does
not exist **[U]**. The newer `speaker-diarization-community-1` card does re-benchmark "legacy
(3.1)" on more sets, including **Ego4D dev at 51.2 DER** and CALLHOME at 28.5 **[P]**.

Now the part that decides whether any of this transfers outdoors:

| Condition | DER % | Mark | Source |
|---|---:|---|---|
| AMI close-talk headset (pyannote 3.1) | 18.8 | [P] | model card |
| AMI distant array1 ch. 1 (pyannote 3.1) | 22.4 | [P] | model card |
| AMI close→distant, pyannote 2.1 default | 18.9 → 27.1 | [R] | Bredin, INTERSPEECH 2023 Tab. 1 |
| DIHARD III track 1 (oracle SAD) baseline / best | 20.65 / 13.45 | [R] | Ryant et al. Tab. 3 |
| DIHARD III track 2 (system SAD) baseline / best | 27.34 / 19.37 | [R] | Ryant et al. Tab. 4 |
| DIHARD III worst three domains (meeting/web/restaurant), median track-1 | 35–45 | [R] | Ryant et al. §6 |
| CHiME-6 track 2 far-field dinner party, baseline | 63.4 dev / 68.2 eval | [R] | Watanabe et al. Tab. 3 |
| CHiME-6 best submitted (USTC) | 56.69 dev / 65.37 eval | [R] | Du et al. Tab. 2 |
| CHiME-7 DASR baseline, CHiME-6 scenario | 40.0 dev / 56.3 eval | [R] | Cornell et al. Tab. 2 |
| CHiME-7 DASR baseline, Mixer 6 scenario | 16.6 dev / 9.3 eval | [R] | Cornell et al. Tab. 2 |

**The spread between two distant-microphone scenarios in the same baseline system, in the same
year, is 47.0 DER points** (56.3 vs 9.3, eval) **[R]**. That is the number to carry out of this
section. The 12–25 % band on the model card is indoor, mid-field, speech-dominated material. The
nearest published analogue to a pole-mounted outdoor microphone — CHiME-6 distant arrays — sits
at **56–68 % DER** for this class of pipeline. **No published pyannote evaluation on outdoor or
ecoacoustic audio exists** other than the ecoVAD comparison in §6.7 (§9, open item 2).

### 2.6 Deployment and licence constraints

| Constraint | Finding | Mark |
|---|---|---|
| `segmentation-3.0` licence | MIT, per HF metadata and card | [P] |
| `speaker-diarization-3.1` licence | MIT | [P] |
| **Gating** | Both are **gated**: requires accepting user conditions + an HF token. Verified empirically — an unauthenticated `curl` of the raw README returns "Access to model … is restricted." | [P] |
| Gating price | The consent text says pyannote "will occasionally email you about premium models and paid services" — acceptance hands over an email address | [P] |
| **Embedding licence mismatch** | `wespeaker-voxceleb-resnet34-LM` is **CC-BY-4.0**, not MIT. The end-to-end 3.1 pipeline is MIT segmentation + CC-BY-4.0 embeddings | [P] |
| Telemetry | pyannote.audio ≥4 ships **opt-out** telemetry on `from_pretrained`, controlled by `PYANNOTE_METRICS_ENABLED` | [P] |
| Published RTF | ~2.5 % — ≈1.5 min per hour of audio — on a **V100 GPU + Cascade Lake 6248**. The 3.1 card drops this sentence; **no official CPU-only RTF is published** | [P] / [U] |
| Commercial path | pyannoteAI: Developer €19/mo, Starter €99/mo, Enterprise/on-prem custom | [P] |

For a project whose stated direction is a self-hosted platform with no external dependency, three
of these rows are structural: a **gated** download, **opt-out telemetry**, and a **CC-BY-4.0**
component in a pipeline described as MIT. The gate and the telemetry are solvable (pre-fetch the
weights into the object store; set the env var; the VAD path drops the CC-BY-4.0 component
entirely). They are solvable *deliberately*, which means they belong in a decision record, not in
a requirements file.

---

## 3. Lightweight VAD: Silero, WebRTC, and the thing that is not FastVAD

### 3.0 ⚠️"FastVAD" is not a project

The brief named FastVAD as a benchmark peer. **It is not one.** A repository search returns only
hobby projects: `Donat24/FastVAD` (3 stars, "a fast VAD based on a GRU"), `Donat24/FastVADCode`
(1), `MXASoundNDEv/FastVAD` (0), `andrestubbe/FastVAD` (0, a JVM wrapper around Silero-ONNX and
WebRTC) **[P]**. None has a model card, a paper, or adoption. **Do not cite FastVAD as a
baseline anywhere in this repository.** The falsified premise is kept here rather than deleted,
because otherwise it will be re-introduced. The established lightweight alternatives that should
occupy that slot are in §3.3.

### 3.1 ⚠️Silero VAD v5 is superseded

The brief specified v5. **v5.0 shipped 2024-06-27 and has been superseded by the v6 line: v6.0
on 2025-08-26, v6.1 2025-11-05, v6.2 2025-11-06, and v6.2.1 on 2026-02-24** **[P]**. Anything
written against "Silero v5 is current" is wrong as of this document's date. v6.2.1's headline
change is directly relevant here: **onnxruntime became an optional dependency** **[P]**.

Version deltas **[P]**, all from release notes:

- **v5.0**: 3× faster TorchScript, ~10 % faster ONNX, model size ~1 MB → ~2 MB, "6000+
  languages", fixed window sizes (`window_size_samples` deprecated), ONNX opset 16.
- **v6.0**: "16 % less errors on noisy real-life data; 11 % less errors on multi-domain
  validation"; new training algorithm.
- **v6.2**: reworked training paradigm, edge cases (child/cartoon/muted voices, low-quality phone
  calls); **"no metrics update"**.
- **v6.2.1**: ONNX Runtime optional.

### 3.2 The comparison table

Everything marked **[V]** below comes from the Silero wiki, which is a vendor comparing itself to
competitors. §3.5 states why that matters.

| Property | Silero (v5/v6) | WebRTC VAD | TEN VAD | pyannote segmentation-3.0 |
|---|---|---|---|---|
| Model artifact on disk | JIT **2,272,526 B (2.17 MB)**; ONNX **2,327,524 B (2.22 MB)**; fp16 ONNX 1,280,395 B; 16 k safetensors 1,239,748 B **[P]** | none — hardcoded fixed-point GMM tables **[P]** | ONNX **315,449 B (308 KB)**; lib ~306 KB (x64) **[P]** | not published **[U]**; PyTorch runtime ≫ 50 MB |
| Parameter count | **not published** **[U]** | n/a | not published **[U]** | **not published** **[U]** |
| Sample rates | 8 k / 16 k (and integer multiples of 16 k) **[P]** | 8/16/32/48 k, 16-bit mono PCM **[P]** | **16 k only** **[P]** | 16 k **[P]** |
| Frame / window | **fixed** 512 @16 k, 256 @8 k = **32 ms** **[P]** | 10 / 20 / 30 ms **[P]** | hop 160/256 = 10/16 ms **[P]** | **10 s** window **[P]** |
| Threshold control | yes (float threshold) **[P]** | aggressiveness 0–3 **[P]** | float probability **[P]** | `argmax` by default; soft path exists **[P]** |
| Per-chunk CPU latency | v5 ONNX **189 µs**, v5 JIT **325 µs**, v4 ONNX 207 µs, v4 JIT 830 µs — Threadripper 3960X, **1 thread**, batch 1, 16 k, 31.25 ms chunk **[V]** | not published **[U]** | RTF **0.0086–0.057** across 5 CPUs **[V]** | GPU RTF 2.5 % for the 3.0 *pipeline*; segmentation alone unpublished **[P]/[U]** |
| Derived CPU per second of audio | v5 ONNX ≈ **5.9 ms/s** (RTF ≈ 0.0059, 165× real time); v5 JIT ≈ 10.2 ms/s (96×) **[V]**, derived | **[U]** | 8.6–57 ms/s **[V]** | **[U]** |
| Licence | **MIT** (LICENSE file + PyPI classifier; a stale badge alt-text still says CC BY-NC) **[P]** | wrapper **MIT**, engine **BSD-3-Clause** **[P]** | Apache-2.0 **with an Agora non-compete clause** — not OSI-open **[P]** | MIT, **gated** **[P]** |
| Maintenance | active (last release 2026-02-24) **[P]** | **`py-webrtcvad` last commit 2021-02-15**; upstream VAD is legacy **[P]** | ONNX opened 2025-06 **[P]** | active **[P]** |

Derivation of the "CPU per second of audio" row, stated so it can be checked: 189 µs per 31.25 ms
chunk × 32 chunks/s = 6.05 ms/s; the wiki's own "165× real time" gives 6.06 ms/s. Rounded to
5.9–6.1 ms/s. **On a Threadripper 3960X core, not on an ARM64 gateway** (§9, open item 3).

Applying the input contract from §1 to a single 5.0 s clip:

| Model | Frames per clip | Derived single-thread CPU per clip | Mark |
|---|---:|---:|---|
| Silero v5 ONNX @16 k | 156 full 512-sample chunks + 128-sample remainder | ≈ **29.5 ms** | [V], derived |
| Silero v5 JIT @16 k | same | ≈ 50.7 ms | [V], derived |
| WebRTC @16 k, 30 ms | 166 frames + remainder | unpublished | [U] |
| TEN VAD @16 k, 16 ms hop | 312 hops | 43–285 ms | [V], derived |
| pyannote segmentation | **1 window, after padding 5.0 s → 10 s** | unpublished on CPU | [U] |

The "<2 MB vs >50 MB" framing in the brief is directionally right but the boundary is not where
it was drawn: **Silero is 2.17–2.22 MB on disk, TEN VAD is 308 KB, WebRTC is zero** (no model
file at all), while anything PyTorch-hosted — pyannote, Whisper, any SSL encoder — pays tens to
hundreds of megabytes of *runtime* before the weights. **On-disk size is not RAM.** ONNX Runtime
session arenas, the decoded audio buffer and the Python interpreter dominate a 2 MB model, and
none of that was measured here (§9, open item 3).

### 3.3 The alternatives that should have been in the brief's third slot

| Candidate | Size / params | Licence reality | Metrics | Mark |
|---|---|---|---|---|
| **TEN VAD** | 308 KB ONNX, ~306 KB lib | Apache-2.0 **plus** a clause forbidding deployment that "competes with Agora's offerings" — **source-available, not OSI-open** | vendor PR curves vs WebRTC/Silero on a LibriSpeech/GigaSpeech/DNS testset released in-repo; RTF table over 5 platforms | [P]/[V] |
| **NVIDIA NeMo MarbleNet** | 1D time-channel-separable CNN, "~1/10-th the parameter cost" of prior SOTA; exact count not fetched **[U]** | NeMo toolkit **Apache-2.0**; individual `.nemo` checkpoints may differ **[U]** | arXiv:2010.13886 | [P]/[U] |
| **FunASR FSMN-VAD** | FSMN streaming VAD | **MIT** | widely used in Paraformer stack; no independent table fetched | [P] |
| **Picovoice Cobra** | n/a | bindings Apache-2.0 but the **engine is proprietary and AccessKey-gated** — **refuse**: it phones a vendor console | vendor only | [P] |
| **pyannote segmentation as VAD** | §2.3 | MIT, gated | §2.5 | [P] |

Cobra fails this project's self-hosting requirement outright. TEN VAD's non-compete clause is the
same class of problem that got BirdNET refused over CC BY-NC-SA: it is not a licence this
repository can adopt without a decision record saying so on purpose.

### 3.4 ⚠️The environmental false-positive number, which does not exist

This is the highest-risk property for this use case and the weakest evidence in the field.

**What is published** — Silero's wiki reports "entire audio accuracy" on **ESC-50** (50
environmental classes including rain, wind, insects, birds), where a clip counts as
false-triggered if ≥100 ms of contiguous speech is predicted **[V]**:

| System | ESC-50 whole-clip accuracy | Implied environmental false-fire rate |
|---|---:|---:|
| **WebRTC** | **0.00** | **~100 %** |
| Silero v3 | 0.51 | ~49 % |
| Silero v4 | 0.51 | ~49 % |
| Silero v5 | 0.61 | ~39 % |
| **Silero v6** | **0.87** | **~13 %** |
| TEN VAD | 0.42 | ~58 % |
| FireRed VAD | 0.60 | ~40 % |

And ROC-AUC on Silero's "Multi-Domain Validation" (17 h) **[V]**: WebRTC 0.73, Silero v3 0.92,
v4 0.91, **v5 0.96, v6 0.97**, TEN VAD 0.93, FireRed 0.94, "unnamed commercial VAD" 0.93.
Accuracy at tuned threshold: WebRTC 0.74, v4 0.85, v5 0.91, v6 0.92, TEN 0.87 **[V]**.

Three readings, in descending order of confidence:

1. **WebRTC is disqualified for outdoor use.** 0.00 on ESC-50 means it flags essentially every
   environmental clip as speech at the tuned threshold. Its GMM assumes quasi-stationary noise;
   wind and dawn chorus are not that. Combined with an unmaintained wrapper (last commit
   2021-02-15) and a legacy upstream, it survives in this survey only as a *floor*.
2. **Even the best available system false-fires on ~13 % of environmental clips.** In a
   delete-on-suspicion design that is a 13 % clip loss against exactly the material this fleet
   exists to collect.
3. **None of this is a field measurement.** ESC-50 is curated isolated sounds; the metric is
   coarse ("≥100 ms anywhere ⇒ whole clip"); the datasets behind the multi-domain column include
   **private** sets (0.5 h private noise, 3.7 h private speech) that no third party can
   reproduce **[V]**.

Silero's own v6.0 notes name the residual failure mode: "music with human voice-like instruments,
very high pitched voices" **[P]**. Birdsong and orthopteran stridulation occupy exactly that
tonal/pitch territory. **This is a stated reason to expect the field FPR to be worse than ESC-50,
not better**, and this survey declines to put a number on it (§9, open item 5).

### 3.5 Why the benchmark table above cannot be trusted at face value

Stated plainly, because it would otherwise be quoted as if it were a leaderboard:

- The comparison is **vendor-produced**. Silero publishes the only comprehensive head-to-head
  table, and it compares Silero to its competitors.
- The optimal threshold is **found on the same Multi-Domain Validation set used to report**, then
  applied to the other sets **[V]** — a self-favouring protocol.
- Part of the data is **private** and unreproducible **[V]**.
- One competitor is anonymous ("unnamed commercial VAD") **[V]**.
- WebRTC, the weakest entry, is configured by whoever is running the comparison; TEN runs it in a
  "pitch-based" configuration in its own table.
- **No peer-reviewed, third-party head-to-head with AUC/F1/RTF across Silero, WebRTC and peers on
  a common public dataset was found** **[U]**. TEN's table has the same conflict of interest in
  the opposite direction.

The correct use of §3.2 and §3.4 is to *order* candidates for our own bake-off, not to predict
our numbers.

### 3.6 Runtime portability

- **ONNX Runtime ships official `manylinux_2_28_aarch64` wheels** (v1.30.0, cp311–cp314 incl.
  free-threaded `cp313t`/`cp314t`), plus macOS/Windows arm64; licence **MIT** **[P]**. `pip
  install onnxruntime` works on an ARM64 gateway with glibc ≥ 2.28.
- int8 dynamic/static quantization via `onnxruntime.quantization` (QDQ/QOperator) is available;
  the exact doc page was not fetched this session **[U]**.
- Threading: `SessionOptions.intra_op_num_threads` / `inter_op_num_threads`; Silero pins both to
  1 for its single-thread benchmark **[P]**. For a 2 MB model, one intra-op thread per stream
  avoids oversubscription on a small gateway.
- **Not on the MCU.** ONNX Runtime is not an ESP32-S3 target (§1, consequence 3).

---

## 4. Whisper encoders as a speech-presence detector

The brief's idea — run a tiny Whisper *encoder* to verify voice presence without decoding
transcripts — is sound in principle and has published support. It also has a specific trap.

### 4.1 The models

| Model | Params | d_model | Enc/dec layers | Heads | Mark |
|---|---:|---:|---:|---:|---|
| whisper-tiny | **39 M** | 384 | 4 / 4 | 6 | [P] |
| whisper-base | **74 M** | 512 | 6 / 6 | 8 | [P] |

Relative speed vs large: tiny ~10×, base ~7×; ~1 GB VRAM each **[P]**. **Licence is not one
thing**: the GitHub repo is **MIT**, while the HuggingFace cards declare **apache-2.0** **[P]**.
Cite whichever artifact you actually ship.

Export sizes **[P]**:

| Artifact | fp32 | int8 / quantized |
|---|---:|---:|
| `Xenova/whisper-tiny.en` ONNX encoder | 32.9 MB | **10.1 MB** |
| `Xenova/whisper-tiny.en` ONNX decoder (merged) | 118.6 MB | 30.7 MB |
| sherpa-onnx `tiny.en` encoder / decoder | 36 MB / 185 MB | 12 MB / 105 MB |
| whisper.cpp `tiny` / `base` ggml | 75 MiB / 142 MiB | `q5_0` available |

**The encoder alone is 10–37 MB — an order of magnitude more than Silero, for a task Silero
already does.** That is the cost line for §8.

### 4.2 The evidence that a frozen encoder carries non-lexical audio information

**Whisper-AT** froze the entire Whisper model, extracted per-layer representations, and trained a
linear head for audio tagging: best config reaches **32.8 mAP AS-20K, 41.5 mAP AS-2M, 91.7 %
ESC-50**, described as **42× faster and 11× smaller than AST**, with the tagging head at
**7.2 M params** and "<< 1 % extra computational cost" over ASR **[R]**. This is the strongest
published evidence that the Whisper encoder is a usable frozen feature extractor for
speech-presence and sound-class decisions.

**WhisperSeg** repurposes Whisper for human *and animal* VAD, reporting segment-wise F1
**0.96124** and frame-wise **0.97789** on its Mouse test subset, with a CTranslate2 build **4×**
faster **[R]**. Directly adjacent to this fleet's domain — though whether it is strictly
encoder-only was not verified **[U]**.

Language ID is available without full decoding: `model.detect_language(mel)` returns the most
probable language token plus the full distribution **[P]**. Note the commonly-quoted "99
languages" is currently ambiguous — `tokenizer.py` holds **100** `LANGUAGES` entries while
`get_tokenizer(num_languages=99)` still defaults to 99 **[P]**. Cite carefully.

### 4.3 ⚠️The trap: Whisper hallucinates speech on silence and non-speech

This is disqualifying for any design that *decodes*, and it is the reason the encoder-only
framing is the right one.

- The Whisper paper itself documents long-form failures including repeat loops and "**complete
  hallucination** where the model will output a transcript entirely unrelated to the actual
  audio" **[R]**.
- The shipped defaults exist because of it: `compression_ratio_threshold=2.4`,
  `logprob_threshold=-1.0`, `no_speech_threshold=0.6` **[P]**.
- *Careless Whisper* (FAccT 2024): **1.4 %** of transcriptions contained hallucinations, and
  **38 %** of hallucinated transcriptions contained harmful content; aphasia speakers 1.7 % vs
  controls 1.2 % **[R]**.
- `whisper.cpp` #1724 reports ~1 s of trailing silence causing transcription of nonexistent
  speech; upstream added `--hallucination_silence_threshold` in PR #1838 **[P]**.
- The ecosystem's own fix is to put a **VAD in front of Whisper**: `whisper-timestamped` runs VAD
  first "to avoid hallucinations", defaulting to **Silero** (licence **AGPL-3.0** — refuse for
  linking into this codebase) **[P]**; **WhisperX** (BSD-2-Clause) defaults `--vad_method
  pyannote` and sets `condition_on_prev_text=False` to reduce hallucination **[P]**, reporting a
  "**nearly twelve-fold speed increase without performance loss**", TED-LIUM WER 10.5 → 9.7 and
  AMI 12.5 → 11.8 **[R]**.

**The field's own consensus is that Whisper needs a VAD, not that a VAD needs Whisper.** For this
fleet that settles the ordering: if Whisper appears at all, it is a **second-stage verifier on
clips a cheap VAD already flagged**, running encoder-only, never emitting text.

### 4.4 The rule that must accompany any Whisper use here

**No transcript may be produced, logged, cached, or uplinked.** A transcript is the most
identifying artifact the system could possibly create — strictly worse than the audio for
retention purposes, because it is small, indexable, searchable and survives every compression
boundary. If a design cannot state a mechanism that makes transcript production impossible rather
than merely disabled, it should not use Whisper.

---

## 5. Self-supervised representations: WavLM, HuBERT, wav2vec 2.0

### 5.1 The models

| Model | Params | Pretraining data | Mark |
|---|---:|---|---|
| wav2vec 2.0 Base | 95 M | LibriSpeech 960 h | [R] |
| wav2vec 2.0 Large | 317 M | LV-60k — **the paper states 53.2 k h after preprocessing** | [R] |
| HuBERT Base | 95 M | LibriSpeech 960 h | [R] |
| HuBERT Large | 317 M | Libri-Light 60,000 h | [R] |
| HuBERT X-Large | 964 M | Libri-Light 60,000 h | [R] |
| WavLM Base | 94.70 M | LibriSpeech 960 h | [R] |
| WavLM Base+ | 94.70 M | **94 k h** = 60 k Libri-Light + 10 k GigaSpeech + 24 k VoxPopuli | [R]/[P] |
| WavLM Large | 316.62 M | 94 k h (same mix) | [R]/[P] |

⚠️Correction to a figure the brief assumed: **"LV-60k" is a corpus name, not an hour count** —
wav2vec 2.0 reports **53.2 k h** after preprocessing **[R]**. The WavLM 94 k h = 60 k + 10 k +
24 k decomposition *is* confirmed verbatim **[R]**.

### 5.2 Speaker-sensitive benchmark results

SUPERB **SD** is diarization on LibriMix, two-speaker, metric DER **[P]**:

| Model | SID acc % | ASV EER % | **SD DER %** |
|---|---:|---:|---:|
| wav2vec 2.0 Large | 86.14 | 5.65 | 5.62 |
| HuBERT Large | 90.33 | 5.98 | 5.75 |
| **WavLM Large** | **95.49** | **3.77** | **3.24** |

WavLM Large is the strongest speech-SSL encoder on speaker tasks by a clear margin **[P]/[R]**.

Layer-wise: for WavLM **Base**, bottom layers carry most speaker information; for **Large**,
middle layers do **[R]**. wav2vec 2.0 shows an acoustic → phonetic → word → *reverse* trajectory,
with acoustic-phonetic content dipping around layers 13–17 in Large-60k **[R]**. Practical
consequence: a speech-presence probe does **not** need the top of the stack, which is where the
lexical content concentrates — a genuine privacy argument for early-exit.

### 5.3 ⚠️Phonetic vs non-speech discrimination: the asymmetry

The brief asked whether these encoders can discriminate speech from non-speech. The published
answer is **they are excellent at speech and materially weak at everything else**. HEAR
leaderboard, ESC-50 / FSD50K **[P]**:

| Submission (encoder) | ESC-50 | FSD50K |
|---|---:|---:|
| GURA Fuse HuBERT (HuBERT X-Large) | 0.7435 | 0.4132 |
| GURA Fuse wav2vec2 (wav2vec 2.0 Large) | 0.6950 | 0.4028 |
| HEAR baseline `wav2vec2` | 0.5610 | 0.3417 |
| PaSST 2lvl (audio-pretrained) | **0.9475** | **0.6409** |
| CED base (audio-pretrained) | **0.9665** | **0.6548** |

And BEATs, audio-pretrained, reaches **98.1 % ESC-50 / 50.6 mAP AudioSet-2M** **[R]**.

**WavLM has no official HEAR row and no ESC-50 table in its paper — the "SSL speech models
underperform on non-speech" finding is verified for HuBERT and wav2vec 2.0 only** **[U]**.

For a *privacy* filter this asymmetry is the right one: sensitivity to speech, indifference to
birds. It also means the same encoder cannot double as the ecology classifier — which matters,
because the existing tagger lane already occupies that role, at 32 kHz, with its own refusals.

### 5.4 Compression, and the licence that fails

| Model | Params | Retention / speed | Mark |
|---|---:|---|---|
| HuBERT Base (teacher) | 94.68 M | SUPERB overall **80.8**, SD 5.88 | [R] |
| **DistilHuBERT** | **23.49 M** (−75.2 %) | SUPERB **75.9** (93.9 % retention), SD **6.19**; CPU feature extraction **992 s → 574 s = 1.73×** ("73 % speedup") | [R] |
| LightHuBERT aBase | 68 M | SUPERB **80.4** (99.5 % retention) | [R] |
| LightHuBERT aSmall | 27 M | SUPERB **79.1** (97.9 %) | [R] |
| **FitHuBERT** | **22.49 M** | 23.8 % of size, 35.9 % of inference time (174.84 s vs 493.72 s); better PR/ASR/KS/ASV than DistilHuBERT but **worse SD (6.84) and SID (55.71)** | [R] |
| 8-bit wav2vec 2.0 | 4× compression, **1262 MB → 354.5 MB** | WER 3.2 % → 3.3 % clean | [R] |

FitHuBERT is the cautionary row: it wins on average while losing on **exactly the
speaker-sensitive tasks a privacy filter cares about**. Average SUPERB score is the wrong
selection metric here.

**4-bit HuBERT/wav2vec2 results could not be verified from any primary source** — only 8-bit
**[U]**.

Licences, applying the BirdNET test:

| Artifact | Licence | Verdict |
|---|---|---|
| fairseq code | MIT **[P]** | ok |
| microsoft/unilm code (contains `wavlm/`) | MIT **[P]** | ok |
| Meta HF cards (`wav2vec2-*`, `hubert-*`) | apache-2.0 **[P]** | ok |
| **WavLM HF cards** (`microsoft/wavlm-*`) | **No SPDX `license` field at all.** The card's "License" link resolves to the UniSpeech LICENSE, whose text is **CC-BY-SA-3.0** **[P]** | ⚠️**not cleanly MIT; legal review before shipping** |

WavLM is the best-performing model in §5.2 and the one with the least clean licence. That
tension should be resolved in a decision record, not in an import statement.

### 5.5 The honest verdict on this family

A 95 M-parameter encoder to answer a one-bit question ("is anyone talking?") is two orders of
magnitude of compute above a 2 MB VAD, for a decision that VAD already makes. The realistic role
of this family here is **offline**: as the reference model against which a cheap on-line gate is
*calibrated*, run once on a held-out, consented, human-reviewed evaluation set — which
`docs/ml-lifecycle.md` §8 already governs, and which does not exist yet (§9, open item 6).

---

## 6. Privacy architecture and legal compliance

⚠️**Not legal advice.** Articles and sections are cited so an engineer can see which design
choices are load-bearing for which obligation. Applicability is fact-dependent — jurisdiction,
operator identity, public-authority status, purpose — and is a question for counsel and a DPIA.

### 6.1 GDPR

| Provision | What it says | Design consequence |
|---|---|---|
| **Art. 4(1)** | "personal data" = any information relating to an identified or identifiable natural person **[P]** | Captured intelligible speech linkable to a person is personal data, full stop. |
| **Art. 4(14)** | "biometric data" = processing of physical/physiological/**behavioural** characteristics "which allow or confirm the unique identification" **[P]** | Voice *can* be biometric. |
| **Art. 9(1)** | prohibits processing "**biometric data for the purpose of uniquely identifying a natural person**" **[P]** | **The qualifier lives in Art. 9(1), not Art. 4(14).** Detecting-and-purging speech without building a voiceprint is **not** Art. 9 processing. Running full diarization (§2.3) moves toward it. This is the single sharpest design lever in this document. |
| **Art. 5(1)(c)** | data minimisation **[P]** | Supports purge-at-source; supports derived-only defaults. |
| **Art. 5(1)(e)** | storage limitation **[P]** | Already implemented as the retention classes in `docs/data-governance.md` §4. |
| **Art. 5(2)** | accountability — controller must "**demonstrate compliance**" **[P]** | **This is the hook that makes deletion receipts valuable rather than decorative** (§6.6). |
| **Art. 25(1)/(2)** | by design and **by default**: "only personal data which are necessary … are processed", covering amount, extent, storage period and accessibility **[P]** | Audio recording default-off; derived-only as the default state. |
| **Art. 35(1), 35(3)(c)** | DPIA required for "**systematic monitoring of a publicly accessible area on a large scale**" **[P]** | An always-on outdoor acoustic network is a textbook trigger. Art. 35(7) fixes the minimum contents. |
| **Art. 6(1)(a) / (f)** | consent vs legitimate interest; **6(1)(f) is unavailable to public authorities performing their tasks** **[P]** | Consent from passers-by is impractical, pushing to 6(1)(f) with a documented balancing test — or 6(1)(e) for public bodies. `docs/data-governance.md` §1 already requires the basis be documented before capture. |
| **Art. 17, Art. 30** | erasure; records of processing **[P]** | Already mirrored by §6 and §1 of the governance doc. |

Guidance: **EDPB Guidelines 3/2019 on processing of personal data through video devices, v2.0,
adopted 29 January 2020** **[P]** — video, but it references "optical or **audio-visual** means"
and flags hidden audio, and is the closest analogical instrument. **There is no dedicated
EDPB/WP29 guideline on audio recording in public space** **[U]** — a genuine gap.

Regulator stance worth quoting to an operator: the **UK ICO** says audio recording of
conversations between members of the public is "**highly intrusive and unlikely to be justifiable
in most circumstances**", is "more privacy intrusive than purely visual recording", and should be
"**switched off by default**" **[P]**.

⚠️**Enforcement claims that could not be verified:** no primary-source EU DPA **fine** squarely
on smart-city acoustic sensors was confirmed. The frequently-cited CNIL intervention regarding
Saint-Étienne's "capteurs sonores" is widely reported in press but the primary CNIL document was
not retrievable **[U]**. The Spanish AEPD has penalised audio-in-CCTV as disproportionate, but a
clean procedure number was not captured **[U]**. Do not cite either as precedent.

### 6.2 CCPA / CPRA

| Provision | Content | Mark |
|---|---|---|
| **§1798.140(v)(1)(H)** | Personal information includes "**Audio**, electronic, visual, thermal, olfactory, or similar information" | [P] |
| **§1798.140(c)** | "Biometric information" includes "**voice recordings, from which an identifier template, such as a … voiceprint, can be extracted**" | [P] |
| **§1798.140(ae)(2)(A)** | **Sensitive** PI includes biometric information processed "**for the purpose of uniquely identifying a consumer**" | [P] |
| **§1798.140(v)(2)(B)(ii)** | "Publicly available" **does not** cover biometric information collected without the consumer's knowledge | [P] |
| **§1798.140(m)** | "Deidentified" = cannot reasonably be linked to a consumer, **plus** reasonable anti-reassociation measures, a public commitment not to re-identify, and contractual binding of recipients | [P] |
| **§1798.140(v)(3)** | Deidentified/aggregate data is excluded from "personal information" | [P] |

⚠️The brief cited §1798.140(v)(1)(B) for audio. That is wrong: **(v)(1)(B) is the §1798.80(e)
cross-reference; audio is (v)(1)(H)**; biometric information is (v)(1)(E) **[P]**.

The structure mirrors GDPR exactly: a raw voice recording is ordinary PI; a **voiceprint used to
identify** is *sensitive* PI. Applicability also requires meeting the "business" thresholds in
§1798.140(d) (≥$25 M revenue, 100 k+ consumers/households, or ≥50 % revenue from selling/sharing
PI) **[P]** — a research or municipal deployment may fall outside.

### 6.3 US biometric statutes

| Statute | Voiceprint covered? | Enforcement | Key duties | Mark |
|---|---|---|---|---|
| **Illinois BIPA**, 740 ILCS 14 | **Yes** — §10 enumerates "voiceprint" | **Private right of action**, §20: **$1,000** negligent / **$5,000** intentional-or-reckless liquidated damages (or actual, whichever greater), plus fees | §15(a) written, **publicly available** retention schedule + destruction guidelines (purpose satisfied or **3 years** from last interaction, whichever first); §15(b) written notice of purpose and term + **written release** before collection | [P] |
| **Texas CUBI**, Tex. Bus. & Com. Code §503.001 | **Yes** | **AG only, no private right of action**; up to **$25,000 per violation** | notice + consent before capture; destroy within a reasonable time, not later than the first anniversary of purpose expiry | [P] |
| **Washington**, RCW 19.375 | **Yes** at §19.375.010(1), **but expressly excludes** "a physical or digital photograph, **video or audio recording** or data generated therefrom" | **AG only**, §19.375.030 | "Commercial purpose" at §19.375.010(4) "**does not include a security or law enforcement purpose**" | [P] |

Two rulings that set the exposure profile:

- ***Cothron v. White Castle System, Inc.*, 2023 IL 128004** (Ill. Sup. Ct., opinion filed
  2023-02-17, rehearing denied 2023-07-18): a separate BIPA claim accrues with **each and every**
  unlawful scan/collection/transmission under §15(b)/(d) **[P]**.
- **SB 2979 → Public Act 103-0769, effective 2024-08-02**: amends §20 so that repeated collection
  of the **same** identifier from the **same** person by the **same** method is "**a single
  violation … entitled to, at most, one recovery**", and allows an electronic signature as a
  written release **[P]**. This legislatively caps *Cothron*'s per-scan accrual.

**Design consequence, and it is the same one as §6.1:** all three statutes regulate a
*voiceprint/template used to identify*, not the existence of audio. Washington says so
explicitly. **A pipeline that never enrolls, never embeds, and never clusters a speaker stays
outside the trigger of all three.** Adding the WeSpeaker embedder from §2.4 walks into it.

⚠️Specific Alexa and McDonald's drive-thru BIPA dockets and outcomes were **not** pinned
**[U]**. Do not cite them.

### 6.4 Wiretap and eavesdropping — the live risk at the moment of capture

- **18 U.S.C. §2510(2)**: "oral communication" means speech "uttered by a person **exhibiting an
  expectation that such communication is not subject to interception under circumstances
  justifying such expectation**" **[P]**. §2510(4) defines "intercept" as the "aural or other
  acquisition" of contents via a device **[P]**. A microphone acquiring speech is an intercept
  **only if** that expectation test is met.
- **California Penal Code §632(a)** makes all-party consent the rule for a "confidential
  communication", fine up to **$2,500 per violation**; **§632(c)** excludes communications "made
  in a public gathering … or in any other circumstance in which the parties … may reasonably
  expect that the communication may be overheard or recorded" **[P]**. §632(d) makes violating
  evidence inadmissible **[P]**. Ambient outdoor capture usually falls in the (c) exclusion.
- Case law on gunshot-detector voice capture **exists and cuts both ways**:
  - ***Commonwealth v. Denison*** (Massachusetts): court **excluded** ShotSpotter audio that
    captured human voices as a prohibited "interception" under M.G.L. c. 272 §99 **[P, via EFF]**.
    ⚠️Appears **unpublished/trial-level**; no reporter citation obtained; the "2007" date in the
    brief is **unsupported** — the documentary trail points to ~2017 **[U]**.
  - ***People v. Johnson*** (California): trial court **admitted** ShotSpotter audio capturing
    voices **[P, via EFF]**. ⚠️Also trial-level and unpublished; the name is extremely common, so
    attach no citation **[U]**.
  - ***People v. Michael Williams*** (Cook County, 2020–21): ShotSpotter first logged the sound as
    a **firework**, an analyst **reclassified** it as gunfire and moved the alert; after a Frye
    challenge prosecutors **withdrew** the evidence and the case was dismissed **[P, via AP]**.
    A reliability lesson, not a voice-capture holding.

**This is the precedent that a detect-and-purge design exists to defuse**, and the reason the
purge has to happen as close to the microphone as physics allows: the theory in *Denison* attaches
at **interception**, which is capture, not dissemination.

### 6.5 Differential privacy for acoustic metrics

- **(ε, δ)-DP**, Dwork & Roth Definition 2.4: for all adjacent datasets x, y and events S,
  `Pr[M(x)∈S] ≤ e^ε·Pr[M(y)∈S] + δ`; pure ε-DP is δ=0 **[R]**.
- **Local vs central.** Central DP assumes a trusted curator over raw data; **local DP**
  randomizes on the device before anything leaves it **[R]**. For a sensor fleet whose whole
  argument is "we do not centralize raw audio", LDP is the matching trust model.
- **RAPPOR** (Erlingsson, Pihur, Korolova, CCS 2014) is the canonical production LDP system;
  its permanent randomized response at f=1/2 corresponds to per-bit ε on the order of
  **ln 3 ≈ 1.1** **[R]**.
- ⚠️**Deployed ε is routinely larger than advertised.** Tang et al. (2017) reverse-engineered
  Apple's macOS/iOS LDP and reported effective per-datum/day budgets around **ε ≈ 6** on macOS
  and up to **≈14** on iOS 10.1.1, with further loss across data types **[R]**. Any ε this
  project publishes must be an *end-to-end accounted* ε, not a per-mechanism one.
- **Event-level vs user-level.** Event-level DP hides one detection at one instant; user-level
  hides everything attributable to one person across the whole stream **[R]**. For an always-on
  sensor, **user-level is what a person actually cares about and is much harder**; claiming DP
  without saying which level is claiming nothing.
- **DP under continual observation**: Dwork, Naor, Pitassi, Rothblum (STOC 2010) introduce the
  continual-release model and the **binary-tree mechanism** for running counts with polylog
  error; Chan, Shi, Song (ICALP 2010 / TISSEC 2011) give the companion binary-counting
  construction **[R]**. These are the right primitives for streaming acoustic event counts.
- ⚠️**There is no canonical "DP for acoustic monitoring" standard** **[U]**. The defensible
  posture is narrow: emit only DP-protected **aggregates** (counts, levels) under a
  continual-observation mechanism with a stated level and a stated, accounted ε — and never treat
  DP as a substitute for not keeping the audio.

Note the interaction with an existing contract: `docs/phase6-list-read-comparison.md` already
specifies **k-anonymous aggregate receipts**, and k-anonymity and DP are different guarantees.
Adding DP would be a change to that contract, not an implementation detail inside it.

### 6.6 Verifiable deletion, and what a receipt can honestly mean

- **NIST SP 800-88 Rev. 1** (Dec. 2014) defines **Clear / Purge / Destroy** and treats
  **Cryptographic Erase** as a Purge technique — destroy the key, and the ciphertext at rest is
  unrecoverable **[P]**. This is the mechanism that makes deletion tractable on wear-levelled
  flash, and it is already the shape of `docs/phase3-object-encryption-contract.md` (revocation
  as crypto-erasure).
- **Proofs of Secure Erasure**: Perito & Tsudik, IACR ePrint **2010/217** (ESORICS 2010) — a
  verifier can gain assurance that a low-cost embedded device with no secure hardware and no
  tight timing has **erased its memory**, by filling it with verifier-supplied randomness and
  proving it **[R]**. Directly applicable to attesting a purge on an MCU-class node.
- **Tamper-evident logging**: **RFC 6962** §2.1 Merkle Hash Trees, §2.1.1 audit paths, §2.1.2
  consistency proofs — the canonical append-only log with inclusion and non-rewrite proofs
  **[P]**. A deletion-receipt log wants exactly this shape.
- **Timestamping**: **RFC 3161** TSP gives non-repudiable "this hash existed at time T" **[P]**.
- **MCU root of trust**: ESP32 Secure Boot + Flash Encryption can attest firmware integrity and
  protect keys, complementing PoSE **[P]** (chip/revision dependent).
- **Auditor-facing controls**: NIST SP 800-53 Rev. 5 **MP-6** (+ MP-6(1)/(2)), **AU-9/AU-10**;
  ISO/IEC 27001:2022 **A.7.10**, **A.8.10**; ISO/IEC 27040 **[P]**.

⚠️**And the limit, which must be stated wherever a receipt is shown to anyone:** deletion is only
as strong as control over *every* copy — RAM, caches, wear-levelled flash blocks, DMA buffers,
backups, derived aggregates. Flash wear-levelling means logical overwrite ≠ physical erase, which
is precisely why NIST recommends crypto-erase over overwrite on flash **[P]**. **You cannot
cryptographically prove a negative.** A deletion receipt is evidence of a *performed, logged,
attested erasure process* — not proof that no bit survives anywhere. `docs/data-governance.md` §6
already requires signed deletion receipts and a reconciler for orphaned copies; this section is
the reason both are needed, and the reason the receipt's wording matters.

### 6.7 Prior art: this has been done, twice, and both results are sobering

**ecoVAD** (Cretois et al., *Methods in Ecology and Evolution*, doi:10.1111/2041-210X.14005) is
the single most on-point publication: a neural model to "detect and remove speech from audio
data" in ecoacoustic recordings, trained by injecting LibriSpeech speech into ecoacoustic and
anthropogenic backgrounds, evaluated with speech played back at **1, 5, 10 and 20 m** from an
**AudioMoth v1.1.1** **[R]**:

| System | mean F1 | Mark |
|---|---:|---|
| **ecoVAD** | **0.917** | [R] |
| pyannote | 0.890 | [R] |
| WebRTC VAD | 0.876 | [R] |

with the finding that "all models could detect human speech with high accuracy at distances where
the speech was intelligible (**up to 10 m**)" **[R]**. The software ships wrappers around pyannote
and WebRTC **[P]**. ⚠️The per-distance F1 table could not be read (Wiley full text blocked)
**[U]**.

Two things to take from it. First, **"intelligible" and "detectable" degrade together with
distance** — which is the most favourable possible fact for this use case, because the speech the
detector misses at 20 m is speech that is largely unintelligible anyway. That is a mitigation, not
an exemption; it is an argument to make in a DPIA with our own distance measurements, not with
theirs. Second, **WebRTC's F1 0.876 here versus its 0.00 ESC-50 whole-clip accuracy in §3.4 is not
a contradiction** — different metric, different material, wildly different operating point. It is
a demonstration of how far VAD numbers move with the protocol, and a reason not to trust any
single table.

**Silent Cities** (*Scientific Data*, doi:10.1038/s41597-024-03611-7) is the operational
precedent, and it published its operating point **[R]**:

> recordings were made at home during lockdown, "human voices are likely to be heard and speakers
> may be easily identified", so they "identified audio segments containing speech and **only
> shared in open access the audio segments without speech**".

Using a general-purpose VAD, **at a true-positive rate of 75 % they accepted an average
false-positive rate of 34 %, rejecting 2,868,098 ten-second segments ≈ 18 % of the dataset**
**[R]**. They additionally coarsened site coordinates to city/neighbourhood level **[R]**.

**That is the honest shape of the trade: to catch three-quarters of speech, they threw away 18 %
of their science.** Any claim this project makes about speech purging should be stated in those
two numbers — a TPR target and the corpus cost — rather than as an F1.

**SONYC** (Bello et al., *CACM* 62(2), Feb. 2019) is the architectural precedent: on-node
processing, short snippets at random intervals, lossless FLAC, "encrypted using 4096-bit AES …
and RSA", uploaded over a VPN at one-minute intervals **[P]**. Its companion voice-anonymization
work (MLSP 2019) separates voice from non-voice with a **deep U-Net**, obfuscates it (low-pass to
remove formants; MFCC inversion), and remixes the blurred vocal content back into the scene
**[P]** — preserving the acoustic scene instead of deleting the window. ⚠️Numeric results behind
an IEEE paywall **[U]**. ⚠️No SONYC primary source supports the commonly-repeated claim that it
captures only low frequencies or only features **[U]**.

Ethics framing: Sandbrook et al. (doi:10.1111/csp2.374) argue conservation monitoring tools that
incidentally collect data on humans are properly understood as **surveillance technologies**, with
principles including "engage with and seek consent from people who may be observed" **[R]**.

Adjacent tooling, for completeness: **BirdNET** does ship `Human vocal`, `Human non-vocal` and
`Human whistle` among its 11 non-event classes **[P]** — a cheap first-pass screen on a model this
project already evaluated — but **no official BirdNET privacy workflow and no published
speech-detection recall for it exist** **[U]**, and its weights were already refused here on
licence grounds. **OpenSoundscape, scikit-maad and Perch document no human-speech class and no
redaction workflow** **[P]**. **NatureLM-audio** (BEATs → Q-Former → Llama 3.1-8B + LoRA, code
MIT but gated Llama weights) is not a privacy tool **[P]**.

---

## 7. Privacy threat model

Scope: an autonomous outdoor acoustic sensor node that captures 5.0 s 48 kHz clips to SD and
drains them to a self-hosted service (§1). The asset under threat is **intelligible human speech
and anything derived from it**.

### 7.1 Assets

| ID | Asset | Where it lives |
|---|---|---|
| A1 | Raw acquisition samples | PSRAM ring on the node (~80 s window) |
| A2 | Stored clip WAV | node SD `/clips`, then pool, then object store |
| A3 | Derived features / sketch | node, uplink, `R0-derived` |
| A4 | Model outputs (tags, VAD scores) | pool, `clips/tags.jsonl` |
| A5 | **Any transcript or speaker embedding** | must not exist (§4.4, §6.3) |
| A6 | Deletion receipts and audit log | `R4-audit` |
| A7 | Node location and clock | metadata on every record |

### 7.2 Adversaries

| ID | Adversary | Capability assumed |
|---|---|---|
| ADV-1 | Curious operator | Holds legitimate `operator`/`reviewer` credentials |
| ADV-2 | Compromised gateway | Full read of anything reaching the service |
| ADV-3 | Physical node thief | Holds the SD card and the MCU flash |
| ADV-4 | Legal compulsion | Subpoena over everything retained |
| ADV-5 | Model-side inference | Can re-derive speech content from artifacts believed to be derived-only |
| ADV-6 | Bystander-as-victim | Not an attacker; the person whose voice is captured and who never consented |

### 7.3 Threats

| ID | Threat | Likelihood driver | Mitigation | Residual |
|---|---|---|---|---|
| **T1** | **Speech is persisted to SD before any classifier runs** | Architectural — true today (§1) | On-node gate before write, or a shortened pre-classification window | ⚠️**Unmitigated today.** No on-node speech model exists (§9 item 4). This is the top finding of this survey. |
| **T2** | VAD misses speech (false negative) → voice reaches the pool | Field FPR/FNR unmeasured (§3.4) | Recall-biased threshold; two-stage verify (§8); treat uncertain as unredacted per governance §2 | Nonzero and **unquantified**; ecoVAD suggests misses concentrate where speech is unintelligible (§6.7) |
| **T3** | VAD false-fires on wind/birds → science is deleted | ESC-50 implies ~13 % even for the best system **[V]** | Quarantine-not-delete for the uncertain band; measure before choosing | Silent Cities paid **18 %** of its corpus **[R]** |
| **T4** | A speaker embedding or voiceprint is created | Using the full 3.1 pipeline instead of the VAD path (§2.3) | **Never run clustering/embedding**; forbid the WeSpeaker component | Design-enforceable; must be a test, not a habit |
| **T5** | A transcript is produced or cached | Any Whisper decode path (§4.3, §4.4) | Encoder-only; no decoder weights on the gateway at all | Design-enforceable |
| **T6** | Location + time + a single speech detection re-identifies a bystander | Node coordinates are precise; detections are timestamped | Coarsen coordinates for shared products (Silent Cities precedent); DP aggregates (§6.5); k-anonymity already contracted in Phase 6 | Correlation across modalities (ADS-B, IMU, tags) is **not analysed here** |
| **T7** | Deletion is believed but not achieved (flash wear-levelling, backups, caches, derived copies) | Physics of flash + replica fan-out | Crypto-erase per NIST 800-88; PoSE attestation; reconciler in governance §6 | ⚠️**Cannot be proven** (§6.6). Receipts attest process only. |
| **T8** | Audio is intercepted at capture, before any purge | The *Denison* theory attaches at interception (§6.4) | Purge as close to the microphone as possible; recording indicator per governance §1; documented lawful basis | Legal, not technical; needs counsel + DPIA |
| **T9** | Model weights or a gated download phone home | pyannote ≥4 opt-out telemetry; Cobra AccessKey (§2.6, §3.3) | Pre-fetch weights to the object store; `PYANNOTE_METRICS_ENABLED` off; refuse key-gated engines | Verifiable by egress policy |
| **T10** | An adversary infers speech content from "derived-only" features | ADV-5; SSL/embedding invertibility is an active research area | Publish what the 20-band sketch can and cannot reconstruct; never store speech-band SSL embeddings | **Not analysed**; the sketch has never been tested for speech reconstructability (§9 item 7) |
| **T11** | Licence contamination forces a takedown of a deployed model | CC-BY-SA WavLM, non-compete TEN VAD, AGPL whisper-timestamped, CC-BY-4.0 WeSpeaker | Licence gate in the decision record, as was done for BirdNET | Enforceable at review time |
| **T12** | Retention class drift — a clip kept "for calibration" outlives its class | Human process | Governance §4 clocks from capture time; exception queue instead of silent extension | Process control, audited |

### 7.4 Trust boundaries

1. **Microphone → PSRAM ring.** No software boundary. Everything after this is mitigation, not
   prevention. T8 lives here.
2. **Ring → SD write.** **The only place a true zero-retention claim could be made**, and the
   only boundary this fleet has no model for. T1 lives here.
3. **SD → drain/uplink.** Governance §2's "before uplink and before durable storage where
   technically possible" attaches here. A gateway VAD sits *after* it.
4. **Service → operator/export.** Access control, tenant boundary, k-anonymity, DP.
5. **Anything → training corpus.** `docs/ml-lifecycle.md` §8 already forbids raw audio without
   dataset approval.

### 7.5 What "zero retention" may and may not mean here

A claim of the form "this system does not retain human speech" is only honest if it names the
boundary. Three defensible claims, in descending strength, and they are not interchangeable:

- **Z1 — never written:** speech never reaches persistent storage. Requires an on-node gate at
  boundary 2. **Not achievable today.**
- **Z2 — never disseminated:** speech may touch node SD but never leaves the node in a
  reconstructable form, and the local copy is crypto-erased on a bounded clock. Achievable with a
  gateway or on-drain filter plus §6.6 machinery.
- **Z3 — never retained beyond class:** speech is subject to the same retention clock as
  everything else and is deleted on schedule with a receipt. **This is what exists today**, and it
  is materially weaker than either of the others.

Publishing Z1 language while implementing Z3 would be the worst outcome in this document.

### 7.6 The operating point the asymmetry forces

A privacy gate is **not** an F1 problem. The costs are not symmetric and not commensurable:

- A **false negative** leaks a bystander's voice into a store, an export, and possibly a subpoena.
  It is irreversible and it belongs to someone who never consented.
- A **false positive** deletes an acoustic event this fleet exists to record. It is expensive and
  it belongs to us.

So the operating point is chosen on **recall at a stated corpus cost**, exactly as Silent Cities
did (75 % TPR, 34 % FPR, 18 % of the corpus gone) — with one improvement available to us:
governance §2 already says uncertain redaction must be treated as unredacted data, which permits a
**three-way outcome** instead of a binary one:

| Outcome | Action | Retention |
|---|---|---|
| clear-of-speech | keep clip | `R1-clip` normal |
| **uncertain** | **quarantine**: keep under the stricter class, no export, no training use, review or expire | `R2-raw` (7 d) |
| speech-detected | purge with receipt | receipt only, `R4-audit` |

That middle row is what buys recall without paying the full Silent Cities corpus cost — and it is
only legitimate because the stricter retention rule for uncertain data already exists in policy.

---

## 8. What a candidate architecture would look like (not a proposal)

Stated only so that §9's open measurements have something to be open *about*. No stage below is
approved, and stage 0 is the one that matters.

| Stage | Where | Candidate | Why | Blocked on |
|---|---|---|---|---|
| **0** | **Node, before SD write** | **none identified** | The only place Z1 is reachable (§7.5) | No ESP-DL/TFLite-Micro speech gate evaluated; PSRAM and duty-cycle budget unmeasured (§9 item 4) |
| 1 | Gateway, on drain | Silero **v6** ONNX int8, 16 kHz, recall-biased threshold | 2.2 MB, MIT, ~6 ms CPU per second of audio **[V]**, aarch64 wheels **[P]** | Field FPR unmeasured (§9 item 5) |
| 2 | Gateway, on stage-1 *uncertain* only | pyannote `segmentation-3.0` **as VAD only** (never diarization), or a frozen Whisper-tiny encoder probe | Independent architecture and training data; §2.3 keeps it out of Art. 9 / BIPA territory | Gated weights must be mirrored; 5 s → 10 s padding untested; no CPU RTF published |
| 3 | Offline, on the eval set only | WavLM Large or DistilHuBERT probe | Calibration reference for stages 1–2 | Eval set does not exist (§9 item 6); WavLM licence (§5.4) |
| 4 | Every stage | signed, RFC-3161-timestamped deletion receipt in an RFC-6962-style append-only log; crypto-erase for at-rest buffers | Art. 5(2) accountability; governance §6 | `phase3-object-encryption-contract.md` gates are not met |

Two properties are non-negotiable across all of it: **no speaker embedding is ever computed**
(T4), and **no transcript is ever produced** (T5).

---

## 9. Open measurements — what has to be produced before anything is chosen

None of these can be answered by reading more papers.

1. **Parameter counts.** Silero, TEN VAD and pyannote `segmentation-3.0` all decline to publish
   one; pyannote's `config.yaml` is gated, so even the BiLSTM layer count of the released
   checkpoint is unconfirmed. Measure from the artifact.
2. **Any pyannote (or other diarizer) evaluation on outdoor/ecoacoustic audio.** Does not exist
   beyond ecoVAD's F1 0.890. The CHiME-6 → Mixer 6 spread of 47 DER points says the prior is
   worthless without a local measurement.
3. **ARM64/x86 gateway RTF and *resident* memory** for each candidate, at our decimation, with
   ONNX Runtime thread pinning. Every latency figure in §3.2 is from a Threadripper 3960X.
4. **Whether an on-node (ESP32-S3) speech gate is possible at all** — ESP-DL/TFLite-Micro
   candidate, PSRAM budget against the existing 7.68 MB raw-ring request, duty cycle, and power.
   **This is the highest-value open question in the document**, because it is the only route to
   Z1 (§7.5).
5. **Field false-positive rate on this fleet's own material** — wind, rain, dawn chorus, cicada
   and cricket — with the ROC that lets an operator choose the §7.6 operating point. The
   literature does not contain this number for any VAD.
6. **A consented, human-reviewed evaluation set.** Every claim in §7.6 needs ground truth, and
   `docs/clip-pipeline.md` already records that a human-verified set "does not exist yet".
   Building one for *speech* requires the consent and notice machinery of governance §1 first —
   this is a governance task before it is an ML task.
7. **Speech reconstructability of the existing 20-band sketch and of any stored embedding**
   (T10). Nobody has tested whether "derived-only" is derived enough.
8. **Legal confirmation** of the items marked **[U]** in §6: the *Denison*/*Johnson* citations,
   any EU DPA acoustic-sensor enforcement, and the specific voiceprint BIPA dockets.

---

## 10. Source index

**Diarization / segmentation** — pyannote/segmentation-3.0 and speaker-diarization-3.1 and -3.0
and community-1 model cards (huggingface.co/pyannote/…); wespeaker-voxceleb-resnet34-LM card;
pyannote-audio `PyanNet.py` @3.1.1, CHANGELOG, README (telemetry), offline-usage tutorial
notebook; Plaquet & Bredin, "Powerset multi-class cross entropy loss for neural speaker
diarization", arXiv:2310.13025; Bredin, INTERSPEECH 2023 (pyannote.audio 2.1); pyannote.ai
pricing.

**Far-field / challenge results** — Ryant et al., "The Third DIHARD Diarization Challenge",
arXiv:2012.01477 + dihardchallenge.github.io results and overview slides; Watanabe et al.,
"CHiME-6 Challenge", arXiv:2004.09249; Du et al. and Arora et al., CHiME-2020 workshop; Cornell
et al., "The CHiME-7 DASR Challenge", arXiv:2306.13734; Nandwana et al., VOiCES, INTERSPEECH 2019.

**VAD** — github.com/snakers4/silero-vad (LICENSE, releases, Quality-Metrics and
Performance-Metrics wikis), pypi.org/pypi/silero-vad/json; github.com/wiseman/py-webrtcvad
(README, LICENSE, commit history); github.com/TEN-framework/ten-vad (LICENSE, README);
Jia/Majumdar/Ginsburg, "MarbleNet", arXiv:2010.13886 + NVIDIA/NeMo LICENSE;
github.com/modelscope/FunASR; github.com/Picovoice/cobra; pypi.org/pypi/onnxruntime/json.

**Whisper** — openai/whisper README, LICENSE, `transcribe.py`, `decoding.py`, `tokenizer.py`,
PR #1838; whisper-tiny/base HF configs and cards; Radford et al., arXiv:2212.04356; Gong et al.,
"Whisper-AT", arXiv:2307.03183; Koenecke et al., "Careless Whisper", arXiv:2402.08021;
ggml-org/whisper.cpp README + issue #1724; k2-fsa sherpa-onnx Whisper export docs;
Xenova/whisper-tiny.en; SYSTRAN/faster-whisper; linto-ai/whisper-timestamped; m-bain/whisperX.

**SSL representations** — Baevski et al., wav2vec 2.0, arXiv:2006.11477; Hsu et al., HuBERT,
arXiv:2106.07447; Chen et al., WavLM, arXiv:2110.13900 + microsoft/unilm `wavlm/` +
microsoft/UniSpeech LICENSE; SUPERB leaderboard data (superbbenchmark.github.io `Data.js`,
`public.json`); Pasad et al., arXiv:2107.04734; HEAR leaderboard (hearbenchmark.com
`leaderboard.csv`) + hear2021-submitted-models; Chen et al., BEATs, arXiv:2212.09058; Chang et
al., DistilHuBERT, arXiv:2110.01900; LightHuBERT, arXiv:2203.15610; FitHuBERT, arXiv:2207.00555;
8-bit wav2vec 2.0, arXiv:2309.14462.

**Law and policy** — GDPR Arts. 4, 5, 6, 9, 17, 25, 30, 35 (gdpr-info.eu; official text EUR-Lex
CELEX 02016R0679); EDPB Guidelines 3/2019 v2.0 (2020-01-29); UK ICO guidance on video
surveillance; Cal. Civ. Code §1798.140 (leginfo.legislature.ca.gov); Cal. Penal Code §632;
740 ILCS 14 (BIPA) §§10, 15, 20; *Cothron v. White Castle*, 2023 IL 128004 (CourtListener);
Illinois P.A. 103-0769 (SB 2979); Tex. Bus. & Com. Code §503.001; RCW 19.375.010 / 19.375.030;
18 U.S.C. §2510, §2511; EFF Street-Level Surveillance — Gunshot Detection.

**Privacy engineering** — Dwork & Roth, *The Algorithmic Foundations of Differential Privacy*
(Def. 2.4); Erlingsson et al., RAPPOR, arXiv:1407.6981; Tang et al., arXiv:1709.02753; Dwork,
Naor, Pitassi, Rothblum, STOC 2010; Chan, Shi, Song, ePrint 2010/… (ICALP 2010 / TISSEC 2011);
NIST SP 800-88 Rev. 1; NIST SP 800-53 Rev. 5 (MP-6, AU-9/AU-10); ISO/IEC 27001:2022 A.7.10 /
A.8.10; ISO/IEC 27040; Perito & Tsudik, IACR ePrint 2010/217; RFC 6962; RFC 3161; ESP-IDF
security docs.

**Acoustic-monitoring prior art** — Cretois et al., ecoVAD, doi:10.1111/2041-210X.14005 (+
Zenodo 7137250); Silent Cities, doi:10.1038/s41597-024-03611-7; Bello et al., SONYC, *CACM*
62(2) 2019 + arXiv:1805.00889 + MLSP 2019 voice anonymization (doi:10.1109/MLSP.2019.8918913);
Sandbrook et al., doi:10.1111/csp2.374; BirdNET-Analyzer FAQ; NatureLM-audio, arXiv:2411.07186;
OpenSoundscape; scikit-maad; Perch.
