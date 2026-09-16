# Survey — bioacoustic backbone foundation models, and the codecs that would feed them

**Status: survey. Nothing here is built, chosen, or procured.** This document exists to put
numbers under the model names that `docs/acoustic-stack.md` §6.3 and `docs/ml-lifecycle.md`
already spend: Perch 2.0 as the S1 embedding backbone (Tier 3 in
`docs/acoustic-models-bleeding-edge.md`), `mn10_as` as the shipped coarse tagger,
and the still-unwritten question of how a remote node's audio gets to a backbone at all when the
link is Meshtastic, not WiFi.

Every figure is either **quoted from a primary source with the source named**, **computed here
from figures that are** (arithmetic shown), or marked ⚠️**NOT PUBLISHED** / ⚠️**UNMEASURED**.
Where a widely repeated claim turned out to be wrong, the wrong claim is kept and corrected
rather than quietly dropped — §9 is the list, and several of them are claims this repository's
own documents make today.

Audience: whoever has to decide, before S1 starts, *which* backbone the 1536-d dispatch rule in
§6.4 of the acoustic stack is dispatching on, and whether a node that is not on WiFi can ever
contribute to it.

---

## 0. The five findings that change a decision

1. **Perch 2.0 is not a Conformer and has no bat data.** It is an **EfficientNet-B3 CNN, 12M
   parameters**, trained on log-mel over **60 Hz – 16 kHz**, and the paper states that **all bat
   recordings were deliberately removed** from training "as their vocalizations cannot be
   represented using the spectrogram parameters we selected" (arXiv:2508.04665 §2.1). The
   1536-d / Apache-2.0 / 32 kHz facts this repo already asserts are **all confirmed**. The
   architecture guess in the survey request ("Conformer/ResNet") is not.
2. **Perch 2.0's weakest measured axes are exactly this fleet's targets.** HumBugDB (mosquitoes)
   **0.758–0.770 accuracy, below AVES-Bio's 0.810**; RFCX **0.200 mAP**, where **Perch 1.0 beats
   Perch 2.0 under a linear probe** (0.232 vs 0.137–0.141). This is the same shape as the
   Orthoptera failure §6.3 already records. Perch 2.0 wins on birds and on marine transfer; it is
   not a general animal detector.
3. **No published study evaluates any neural audio codec on bioacoustic round-trip fidelity.**
   Searches across arXiv and OpenAlex for EnCodec/DAC/SoundStream against bird, bat, whale or
   insect corpora returned nothing. What exists is about **MP3** (degrades bird detection,
   *Bioacoustics* 2023) and about **learned embeddings surviving compression that hand-crafted
   indices do not** (*Ecology and Evolution* 2021: an AudioSet fingerprint tolerated CBR 64 kb/s
   → 8 % of file size "without any detectable effect"). §7.5 proposes the measurement this fleet
   could run in an afternoon, because nobody else has.
4. **Codec bandwidth, not codec bitrate, is the blocker.** EnCodec 24 kHz reconstructs to a
   **12 kHz** ceiling — it removes the top quarter of Perch's own mel range before Perch ever
   sees it. **DAC at 44.1 kHz (22.05 kHz ceiling, published operating point at 1.78 kbps) is the
   only open-weight, permissively licensed codec whose band covers the backbone's input.** Its
   encoder is 22M parameters, which is not an ESP32-S3 workload under any assumption.
5. **A float32 Perch embedding costs more airtime than the codec-compressed audio it came from.**
   1536 × 4 B = **6,144 B** per 5 s window; the same window at DAC 1.78 kbps is **1,113 B**. Over
   Meshtastic ShortTurbo that is **~2.6 s of airtime versus ~0.5 s** (§7.4). Any design that
   imagines shipping embeddings over the mesh to save bytes has the inequality backwards.

---

## 1. What the fleet actually constrains

These are this repository's own measured numbers, restated because every table below is scored
against them. Sources are in-tree.

| constraint | value | source |
|---|---|---|
| node acquisition rate | **48 kHz**, 16-bit mono | `docs/release-v0.1.6-readiness.md` (`fs_acquisition_hz: 48000`) |
| clip | 5.0 s (1.0 pre / 4.0 post), **480,044 B** | `docs/acoustic-stack.md` §0.3 |
| sketch on the mesh | 172 B payload, **193 B on-air**, ShortTurbo **78.9 ms** | `docs/uplink.md`; `docs/esp32s3-lora-node.md` §7.3 |
| measured event rate | ~118 detections/h/node, 6 nodes → **1.53 % duty for sketches alone** | `docs/esp32s3-lora-node.md` §7.2 |
| hugbot mic ceiling | **4,501 Hz** | `docs/acoustic-stack.md` §7 |
| central GPU | 2 × time-sliced, no VRAM isolation; sizing target **RTX 2080 Ti, sm_75, ~10.7 GB** | `docs/acoustic-stack.md` §6.3 gate 3 |
| disk headroom | **~52 GB** on the ext4 VHDX root | `docs/phase3-storage-capacity-model.md` |
| shipped tagger | EfficientAT `mn10_as`, 4.88M params, 0.54 GMACs, 960-d, MIT | `docs/acoustic-stack.md` §6.2b |
| embedding dispatch rule | every record carries `dim`, `model`, `model_version`, `fs_native`, `fs_source`; consumers dispatch on width first | `docs/acoustic-stack.md` §6.4 |

Two of these decide most of what follows: **48 kHz acquisition means no backbone in this survey
requires upsampling** (all of them want ≤48 kHz), and **~52 GB of disk means continuous embedding
of the fleet is not affordable** (§4.4).

---

## 2. Perch 2.0 — the S1 candidate, measured

Primary source: van Merriënboer, Dumoulin, Hamer, Harrell, Burns, Denton, *"Perch 2.0: The
Bittern Lesson for Bioacoustics"*, [arXiv:2508.04665](https://arxiv.org/abs/2508.04665).
Weights: <https://www.kaggle.com/models/google/bird-vocalization-classifier>, instances
`perch_v2` and `perch_v2_cpu`. ⚠️It is **not** at `kaggle.com/models/google/perch`.

### 2.1 Architecture and I/O

| property | Perch 2.0 | Perch 1.x | source |
|---|---|---|---|
| backbone | **EfficientNet-B3** (CNN; ⚠️no Conformer, no transformer) | EfficientNet-B1 | §2.2 |
| embedding params | **12M** | 7.8M (80.1M with heads) | §2.2; SurfPerch Table 1 |
| classification head | **+91M** (1536 × 14,795) | — | Kaggle card |
| sample rate / window | **32 kHz / 5 s (160,000 samples)** | 32 kHz / 5 s | §2.2, §A.2 |
| frontend | **log-mel**, 128 bins, **60 Hz – 16 kHz** | **PCEN** mel | §2.2, §A.2 |
| STFT | win 20 ms (640) / hop 10 ms (320), FFT 1024, Hann, uncentered, magnitude, HTK mel, log floor 1e-5 ×0.1 | — | §A.2 |
| spatial embedding | **(5, 3, 1536)** (time × freq × features) → mean **1536-d** | 1280-d | §2.2 |
| classes | **14,795** = 14,597 species + 198 FSD50K | 10,932 | §2.1 |
| licence (code and weights) | **Apache-2.0** | Apache-2.0 | Kaggle API `licenseName`; `google-research/perch` |
| format | TF2 SavedModel; ⚠️**no official ONNX**, not on HuggingFace | TF2 SavedModel | Kaggle API |

**Compatibility with this fleet's rates.** Perch wants 32 kHz. The nodes acquire 48 kHz, so the
path is a **decimation of real audio, never an upsample** — which is what closed §6.3's Gate 2
and remains true. 48 kHz → 32 kHz is a 2:3 rational resample; it is not free (⚠️the resampler's
own passband ripple and the aliasing of 16–24 kHz content into the mel range are
**UNMEASURED here**), but it destroys nothing Perch could have used: Perch's mel top is 16 kHz
and a 32 kHz signal's Nyquist is exactly 16 kHz.

**Clustering.** `google-research/perch-hoplite` (Apache-2.0) is the maintained tool — an
embedding database plus approximate search and agglomerative clustering over the 1536-d vectors,
designed for exactly the "search an unlabelled corpus from a handful of examples" workflow that
§6.4 calls the payload. ⚠️The `google-research/perch` README itself cautions that "certain
segments are out of date and pip install is unlikely to work"; hoplite is the supported entry
point. Note the **spatial (5, 3, 1536)** output: the mean-pooled 1536-d vector is a *choice*, and
retaining the 5 time-slices is available if temporal resolution inside a 5 s window is ever
wanted. It costs 5× the storage in §4.4.

### 2.2 Training data provenance

Recordings by taxonomic class (§2.1, Table 1). Xeno-Canto and iNaturalist downloaded **March
2025**, Tierstimmenarchiv **April 2025**; iNaturalist via GBIF "research-grade" audio.

| source | Aves | Amphibia | Insecta | Mammalia | other | total |
|---|---:|---:|---:|---:|---:|---:|
| Xeno-Canto | 860,701 | 2,260 | 31,971 | 1,323 | 0 | 896,255 |
| iNaturalist | 480,230 | 51,450 | 30,535 | 9,074 | 409 | 571,698 |
| Tierstimmenarchiv | 26,622 | 1,341 | 860 | 4,992 | 44 | 33,859 |
| FSD50K | 0 | 0 | 0 | 0 | 40,966 | 40,966 |
| **total** | **1,367,553** | **55,051** | **63,366** | **15,389** | **41,419** | **1,542,778** |

⚠️**eBird and Macaulay are not in it.** The survey request named "xeno-canto + eBird / BioGeo /
MacAulay"; the paper's four sources are the four above. Do not cite Macaulay as Perch 2.0
training provenance.

⚠️**Bats were removed on purpose** (§2.1). ⚠️Marine content is "a few dozen cetacean recordings,
but these were mostly phone recordings made above water and not reflective of underwater
hydrophone recordings" (§C.2) — which makes §2.4's marine transfer result more surprising, not
less.

### 2.3 Training objectives — why the embedding is good

Three losses (§2.3), and the paper's thesis is that **fine-grained supervised classification is
the pre-training signal**, not SSL:

1. **Species cross-entropy** with a softmax target of `1/k` per target class (not sigmoid BCE).
2. **Self-distillation**: a ProtoPNet prototype head (4 prototypes/class, max activation,
   orthogonality loss) is the *teacher*; the dense linear classifier is the *student*; a
   **stop-gradient** keeps prototype gradients out of the embedding.
3. **Source prediction (DIET)**: predict which of >1.5M source recordings a 5 s window came from,
   via a rank-512 low-rank projection. Augmentation is windowing alone.

Plus **generalized mixup** to N>2 components with multi-hot targets. Two phases: ≤300k steps
without distillation, ≤400k with. **20–30 h per model on a TPUv3-8**, hyperparameters by Vizier
over 2 × 100 models.

**Label-granularity ablation (Table 7)** is the load-bearing evidence, Xeno-Canto only:
species (10,906 classes) **0.837** BEANS acc → genus (2,398) 0.820 → family (249) 0.761 → order
(41) **0.608**. Coarsen the labels and the embedding degrades monotonically. This is a direct
argument against the intuition that "any big audio model" is a backbone.

### 2.4 Benchmarks, including what it loses

| model | BirdSet AUROC | BirdSet cmAP | BEANS acc | BEANS mAP |
|---|---:|---:|---:|---:|
| Perch 1.0 | 0.839 | 0.356 | 0.809 | 0.353 |
| **Perch 2.0 (peak-select)** | **0.907** | 0.430 | **0.839 / 0.841** | **0.426 / 0.504** |
| **Perch 2.0 (random window)** | **0.908** | 0.431 | 0.838 / 0.840 | 0.415 / 0.502 |
| BirdMAE-L (fine-tuned) | 0.886 | **0.440** | — | — |
| Audio ProtoPNet-5 | 0.896 | 0.423 | — | — |
| AVES-Bio | — | — | 0.817 (FT) | 0.398 |
| BioLingual | — | — | — | 0.479 (FT) |
| NatureLM-Audio (0-shot) | — | — | — | 0.153 |

⚠️**The SOTA claim rests on AUROC.** BirdMAE-L retains the best cmAP (0.440 vs 0.431), and the
paper says plainly that it believes "ROC-AUC ... is the most stable and informative". Quote the
axis with the number.

**Per-dataset BEANS accuracy, Perch 2.0 (peak, linear probe):** esc50 0.908, watkins 0.900,
**bats 0.774**, cbi 0.789, dogs 0.957, **humbugdb 0.758**, speech 0.789.
**BEANS mAP (peak, prototypical):** dcase 0.457, enabirds 0.764, hiceas 0.585, **rfcx 0.200**,
hainan gibbons 0.516.

**Documented non-avian weaknesses** — read these next to §6.3's Orthoptera finding:

- **Bats 0.774–0.815** — a pure transfer result; bats were removed from training.
- **Mosquitoes (HumBugDB) 0.758–0.770, losing to AVES-Bio (0.810).**
- **Speech Commands 0.789–0.838 vs AVES-Bio 0.964.**
- **RFCX 0.200 mAP, and Perch 1.0 (0.232) beats Perch 2.0's linear probe (0.137–0.141).**

**Marine transfer, k=16 few-shot ROC-AUC (Table 8)** — despite having almost no underwater
training data:

| model | DCLDE species | DCLDE ecotype | DCLDE known-bio | NOAA PIPAN | ReefSet |
|---|---:|---:|---:|---:|---:|
| Multispecies Whale | 0.914 | 0.821 | 0.954 | 0.917* | 0.855 |
| SurfPerch | 0.947 | 0.903 | 0.984 | 0.899 | **0.986*** |
| Perch 1.0 | 0.968 | 0.931 | 0.981 | 0.905 | 0.970 |
| **Perch 2.0** | **0.977** | **0.945** | **0.989** | **0.924** | 0.981 |

(*) trained on that data. **The finding that matters for this fleet:** Google's own multispecies
whale model scores **AUC 0.612 from its logits** on DCLDE but **0.954 from its embeddings + a
16-shot probe**. A pretrained classifier's *answers* and its *representation* are different
products under domain shift. §6.3's "drop the classification head, embeddings only" rule is
independently confirmed by that pair of numbers.

### 2.5 Supply chain — the unresolved gate

`docs/acoustic-stack.md` §6.2b set the rule: hash-pinned at both ends, the job **verifies and
refuses**, it never builds and never downloads a model it cannot hash. Perch 2.0 currently cannot
satisfy that rule through ONNX:

- Kaggle ships **TF2 SavedModel** for all instances (`framework: tensorFlow2`).
- **No official ONNX export exists.** The only ONNX on Kaggle is a third-party community upload
  (`zhiyue666/zhiyue-perchv2onnx`) of unverified provenance — the exact thing the `mn10_as` rule
  was written to exclude. A first-party export (`tools/export_perch_onnx.py`, byte-reproducible,
  with a numerical-agreement assertion like `mn10_as`'s 7.6e-06 / 1.0e-06) is the path that keeps
  the rule; ⚠️whether `tf2onnx` round-trips this graph within tolerance is **UNMEASURED**.
- The **`perch_v2_cpu` instance sidesteps §6.3's Gate 3 entirely** — the "does the sm_75 2080 Ti
  run the chosen runtime" question does not apply to a CPU path. ⚠️Its throughput is **NOT
  PUBLISHED**; at the fleet's ~118 events/h/node this may simply not matter, exactly as the
  48 ms-vs-12 ms tagger argument did not matter.

---

## 3. Self-supervised and language-supervised alternatives

### 3.1 BioLingual — CLAP for animals

Robinson, Robinson, Akrapongpisak, [arXiv:2308.04978](https://arxiv.org/abs/2308.04978).
Weights: <https://huggingface.co/davidrrobinson/BioLingual>.

| property | value | source |
|---|---|---|
| framework | **CLAP** — contrastive language-audio, symmetric InfoNCE, learnable temperature | §2.2 |
| audio encoder | **HTS-AT** (init from `laion/clap-htsat-unfused`), feature fusion disabled | §2.2; HF `config.json` |
| text encoder | **RoBERTa** — ⚠️**not GPT-2** | HF config, RoBERTa BPE tokenizer |
| projection dim | **512** (audio and text) | HF `config.json` |
| sample rate / window | **48 kHz / 10 s** (480,000 samples) | HF `preprocessor_config.json` |
| mel | 64 bins, n_fft 1024, hop 480, **50 Hz – 14 kHz** | HF `preprocessor_config.json` |
| params | ⚠️**NOT PUBLISHED.** Checkpoint is 614,525,833 B; at fp32 that implies **≈153M** — *inference, not a published figure* | HF API |
| training data | **AnimalSpeak, 1,102,307 text-audio pairs** from iNaturalist, Tierstimmenarchiv, Watkins (15,568 recordings), Xeno-Canto; captions from metadata templates + ChatGPT | §2.1 |
| species | ⚠️the paper says **"over 25,000"** in the abstract and **"approximately 28,000"** in §2.1 | §2.1 |
| licence | ⚠️**NOT PUBLISHED for the weights.** No `license` tag on the HF repo. The **code** repo `david-rx/BioLingual` is Apache-2.0; that does not cover weights | HF API; GitHub API |

**What it uniquely buys: real zero-shot.** 68.9 % top-1 across **1,143 species** on 29,134
held-out AnimalSpeak examples, against CLAP-LAION's 0.4 %; text→audio retrieval mAP@10 52.2 %,
precision@1 63.0 % against a theoretical perfect-species-detector ceiling of 64.5 %.
Zero-shot mAP by dataset: watkins 0.257, cbi 0.705, enabirds 0.275, rfcx 0.065, esc50 0.600,
anml 0.689 — and it **loses** to CLAP-LAION on humbugdb (0.085 vs 0.130) and hiceas (0.267 vs
0.421). Fine-tuned on BEANS it beats AVES-Bio on 10 of 11 tasks (bat **0.766**, watkins 0.894,
dcase 0.475).

**Why it is not the S1 backbone, and where it belongs.** 48 kHz native is a *better* fit to this
fleet than Perch's 32 kHz — no resample at all. But the licence gap is disqualifying under
`docs/data-governance.md`'s posture and the §6.3 precedent that refused BirdNET's weights over
CC BY-NC-SA. Its real value here is as a **label bootstrapper**: a text query ("katydid", "dog
bark", "rifle report") against an unlabelled clip cache produces candidate labels for
`tools/hear_annotate` without anyone training anything — the same free-label pattern as
BirdWeather 4066 in §S2. That is an offline, operator-side use where a weights-licence question
is answerable before anything ships.

### 3.2 animal2vec — the one true SSL entry, and its integration tax

Schäfer-Zimmermann et al., [arXiv:2406.01253](https://arxiv.org/abs/2406.01253); now published
as *Methods in Ecology and Evolution* 2026, doi:10.1111/2041-210x.70218 (CC-BY).

| property | value | source |
|---|---|---|
| paradigm | **data2vec 2.0** — mean-teacher self-distillation; teacher = EMA of student | "animal2vec framework" |
| **masking objective** | teacher sees the **complete unmasked** signal and emits target embeddings; student sees the **masked** signal and regresses them with **MSE**. ⚠️Not contrastive, not generative reconstruction | Fig. 3 |
| input | **raw pressure waveform**, learned SincNet-style filterbanks → transformer | intro |
| sample rate / window | **8 kHz / 10 s** (80,000 samples) | GitHub README |
| output resolution | 2000 timesteps × 12 classes per 10 s = **5 ms frames** | GitHub README |
| params | **315M** (`animal2vec_large`) | §4 |
| licence | **code MIT**; **weights public** (Max Planck Edmond, doi:10.17617/3.ETPUKU); **dataset CC BY-NC 4.0** | GitHub; Edmond |

**MeerKAT**: 1,068 h, 384,592 × 10 s samples, **184 h strongly labelled**, one species (meerkat),
41 individuals, 12 classes. Collar recorders at 8 kHz/10-bit and handhelds at 48 kHz/32-bit, all
standardized to 8 kHz/16-bit.

**Result that justifies SSL at all:** macro-AP **0.78** / micro-AP 0.91 at 100 % fine-tune
labels, versus data2vec 2.0's 0.26 / 0.30 — and **0.66 macro-AP with only 1 % of the labels**.
On NIPS4Bplus (birds, pretrained on a 700 h Xeno-Canto subset) it sets SOTA, F1 +0.06 over
DenseNet121.

**Why it is not a candidate here, stated plainly:**

- ⚠️**8 kHz.** A 4 kHz Nyquist is below `mn10_as`'s band and far below Perch's. For the fleet's
  48 kHz audio this is a 6:1 decimation that discards everything above 4 kHz — most of the bird
  content and all of the impulsive high-frequency structure the sketch classifier scores on.
- ⚠️**fairseq pinned to commit `920a548c`, Python 3.6–3.9, pip pinned to 24.0.** The repo's CI
  matrix is **3.12 / 3.13** (`.github/workflows/ci.yml`). This is not a dependency, it is a
  second runtime.
- The authors' own caveat: performance "is contingent on pretraining", and "smaller models, such
  as **SincNet (2.6M parameters)**, remain a viable alternative"; their future-work list names
  "the large variability in different sampling rates" as unsolved.

**What to take instead of the model: the finding.** Teacher-on-clean / student-on-masked with an
MSE target reached 0.66 macro-AP at **1 % labels**. The fleet's binding constraint is labels
(§S2), not architecture. That is the argument for an SSL objective over this fleet's own scene
corpus later — not for importing a 315M-parameter 8 kHz meerkat model now.

---

## 4. Domain extensions — reef, marine, infrasound

### 4.1 SurfPerch

Williams, van Merriënboer, Dumoulin et al., [arXiv:2404.16436](https://arxiv.org/abs/2404.16436);
*Phil. Trans. R. Soc. B* 380(1928):20240280. Weights:
<https://www.kaggle.com/models/google/surfperch>, **Apache-2.0**.

⚠️The title "Towards a General-Purpose Foundation Model for Computational Bioacoustics" used in
the survey request is **not** this paper.

Perch 1.x (EfficientNet-B1, PCEN frontend) domain-adapted to reef audio by **triple-domain**
pretraining — reef + Xeno-Canto birds + FSD50K. **32 kHz, 5 s, embedding 1280-d**, heads
`reef_label[38]`, `fsd50k_label[200]`, `label[10932]`, genus/family/order. **ReefSet**: 57K
annotated 1.88 s recordings, 37 classes, 16 datasets, all resampled to **16 kHz** at assembly.
Few-shot **mean AUC-ROC 0.902 ± 0.09 at 4 samples/class**, final layer only.

**The negative result is the useful one.** SurfPerch is **worse than plain Perch and BirdNET on
all six novel non-reef domains tested** (mean AUC-ROC gap 0.067 at 4 samples/class, narrowing to
0.012 at max samples), and Perch beat it on Watkins marine mammals. Domain adaptation bought reef
performance and sold generality. Any future "fine-tune Perch on the fleet's own corpus" proposal
inherits this result as its prior.

SurfPerch's own baseline table is the cleanest cross-backbone comparison published:

| network | domain | classes | SR kHz | window s | embed | params | CPU real-time factor |
|---|---|---:|---:|---:|---:|---:|---:|
| VGGish | AudioSet | 31,000 | 16 | 0.96 | 128 | 72.1M | 41.1× |
| YAMNet | AudioSet | 521 | 16 | 0.96 | 1024 | 4.7M | 86.0× |
| BirdNET v2.3 | birds | 3,337 | 48 | 3 | 1024 | 10.4M | 260.7× |
| Perch v1.4 | birds | 10,932 | 32 | 5 | 1280 | 80.1M | 39.4× |

Transfer ranking on ReefSet: **BirdNET 0.908 > Perch 0.881 > YAMNet 0.834 > VGGish 0.813**.
⚠️"Calling in Our Corals" is referenced in the ecosystem but its contribution to ReefSet was
**not verified** here.

### 4.2 Marine and low-frequency models with open weights

| model | SR | window | architecture | outputs | licence | notes |
|---|---:|---:|---|---|---|---|
| [Google multispecies whale](https://www.kaggle.com/models/google/multispecies-whale) | **24 kHz** | 5 s | EfficientNet-B0 on spectrograms | 12 (7 species + 5 call types) | Apache-2.0 (+ an AI-principles request in the ToU) | micro-AUC **0.9963** over 314,104 windows; precision @0.5 collapses on orca whistle **0.35** and echolocation 0.59 |
| [Google humpback_whale](https://www.kaggle.com/models/google/humpback-whale) | **10 kHz** | 3.92 s | **PCEN → ResNet-50** → 1 logistic unit | binary | Apache-2.0 | HARP deep-water training; card warns the logit is **not a probability** and needs calibration |

⚠️The multispecies model is **24 kHz, not 10 kHz** — the 10 kHz figure belongs to the
humpback model and to the NOAA PIPAN training archive. And see §2.4: its logits score 0.612 where
its embeddings score 0.954.

### 4.3 Infrasound — there is no foundation model, there is a trick

**The published native-rate work** is Cornell ELP: Bjorck et al., AAAI 2019
([arXiv:1902.09069](https://arxiv.org/abs/1902.09069)). Recorders at **2 kHz/12-bit or
4 kHz/16-bit**; rumble fundamentals **8–34 Hz**, 2–8 s long; the model **downsamples to 1 kHz**,
FFT 512 / hop 384, **discards everything above 100 Hz**, and trains a DenseNet on a
**64 × 47** tensor. Classification accuracy **89.72 %** (per-site 93.40 / 93.68 / 94.30 / 77.51);
conv-LSTM segmentation 91.71 % vs 69.74 % LSTM-only. Keen et al., *JASA* 141(4) 2017:
**83.2 % TPR at 5.5 % FPR ≈ 20 false positives/hour**. ⚠️**ELP publishes no open model weights.**
⚠️"ElephantCallerID" **does not exist** (0 GitHub code-search results); do not cite it.

**Three engineering strategies exist, and only the third is available to this fleet:**

1. **Native low-rate, purpose-built** (ELP): record at 2–4 kHz, discard >100 Hz, train bespoke.
   Highest fidelity; requires in-domain data at ELP scale (>700,000 h archived) to justify.
2. **Sample-rate-agnostic frontend** (DeepSqueak, BSD-3, MATLAB, YOLOv2 since v3.1): spectrograms
   built from **constant-*duration* FFT windows rather than constant sample counts**, so "other
   sample rates are accepted" (project wiki). Tested at 250 kHz for rodent ultrasound. This is an
   architectural answer, not a preprocessing one.
3. **Time-compression / sample-rate re-tagging**: move the signal into the backbone's band.
   BirdNET ships this as `--audio_speed` and documents it explicitly: *"modify the speed of your
   audio to shift the frequency ... This also enables you to train classifiers for ultra- or
   infrasonic signals, i.e. bats or whales"*, with a caution that the same setting must be
   applied at inference. The published worked example is **ultrasonic** (384 kHz → 38.4 kHz at
   speed 0.1). ⚠️The infrasonic direction is documented as a *mechanism* but **not published as a
   configuration** — an inference, not a citable fact.

The one published infrasound application of (3) is `ramayer/elephant-rumble-inference`, which
speeds audio **16×** and up-shifts **4 octaves** to bring rumbles into AVES/HuBERT's 20 ms
feature timescale, reporting a **16× compute reduction** as a side effect (24 h of audio in 22 s
on an RTX 2060). ⚠️Its metrics exist only inside a PNG and its licence was not verified.

**What this means here.** Nothing in the fleet is infrasonic today and nothing should become so
on the strength of this section. But the mechanism is cheap to keep in mind: **a resample plus a
re-tagged rate is the entire adapter** between a band this fleet can record and a backbone
trained elsewhere — and it is a *lossless-in-information* operation that costs one line in a
preprocessing function and one field (`fs_source` vs `fs_native`, which §6.4 already mandates).

### 4.4 Storage arithmetic before anyone embeds anything

1536 float32 = **6,144 B** per 5 s window.

| coverage | per node/day | ×3 nodes/year |
|---|---:|---:|
| continuous, 5 s hop (17,280 windows/day) | **106.2 MB** | **116.3 GB** |
| continuous, int8-quantised + scale | 26.6 MB | 29.1 GB |
| event-gated at the measured 118 det/h (2,832/day) | **17.4 MB** | 19.1 GB |
| continuous, retaining the (5,3,1536) spatial map | 1.59 GB | 1.7 TB |

Against **~52 GB** of free disk (`docs/phase3-storage-capacity-model.md`), **continuous float32
embedding of three nodes exceeds the disk inside six months**, and the spatial map is not
discussable. Event-gated float32 fits. This is an arithmetic constraint on S1's scope, not a
preference.

---

## 5. Masked autoencoders versus the shipped tagger

### 5.1 The comparison table

GMACs are per 10 s of audio where published. ⚠️Every "not published" below was checked in the
paper and the repo, not assumed.

| model | params | GMACs / 10 s | SR | input | embed | AudioSet mAP | licence | weights |
|---|---:|---:|---:|---|---:|---:|---|---|
| **EfficientAT `mn10_as`** *(shipped)* | **4.88M** | **0.54** | 32 kHz | 128 mel, 10 ms hop | **960** | **0.471** | **MIT** | GitHub releases |
| EfficientAT `mn40_as_ext` | 68.4M | 8.03 | 32 kHz | 128 mel | 960×4 | 0.487 | MIT | GitHub releases |
| EfficientAT `dymn10_as` | 10.6M | 0.58 | 32 kHz | 128 mel | — | 0.477 | MIT | GitHub releases |
| PaSST-S | 87M | **~128** (measured by the EfficientAT authors) | 32 kHz | 128 mel, 998 frames | 768 ⚠️inferred | 0.471 (single) / 0.4956 (ensemble) | **Apache-2.0** ⚠️not MIT | GitHub |
| BEATs (iter3+) | 90M | ⚠️not published (<5 % of DyMN-L's ratio claim only) | 16 kHz | waveform | 768 | **0.486** / 0.506 ensemble | MIT | ⚠️OneDrive links only |
| **AudioMAE (ViT-B)** | **86M** | ⚠️**not published** | 16 kHz | 128 Kaldi mel, 1024×128, 16×16 patches (512) | **768** | **0.473** | ⚠️**CC BY-NC 4.0** | Google Drive |
| **CAV-MAE (Scale+)** | 164M enc + 27M dec; **~85M audio-only encoder** | ⚠️**not published** | 16 kHz | 1024×128 fbank, 512 patches | 768 | **0.466** audio-only / **0.512** audio-visual | **BSD-2-Clause** | GitHub |
| Perch 2.0 | 12M (+91M head) | ⚠️**not published** | 32 kHz | 128 mel, 500 frames, 5 s | **1536** | n/a (not an AudioSet model) | Apache-2.0 | Kaggle |

**AudioMAE detail:** masking **0.8 unstructured** at pretraining, 0.3 time / 0.3 freq at
fine-tuning; decoder is a 16-layer **shifted local attention** transformer (47.3 mAP vs 46.8 for
global attention); ESC-50 **94.1 ± 0.10**, SPC-2 98.3, VoxCeleb SID 94.8. The 80 % masking is a
**pre-training** economy — fine-tuned inference still processes all 512 patches, so it buys
nothing at the runtime this fleet cares about.

**CAV-MAE detail:** 0.75 masking on both modalities, `contrast_loss_weight 0.01` /
`mae_loss_weight 1.0`. ⚠️BEATs' comparison table lists "CAV-MAE 86M, 44.9 mAP" — a *different*
(audio-only-encoder, non-Scale+) configuration. Do not mix the two.

### 5.2 The verdict, which §8 of the acoustic stack already reached

`mn10_as` at **0.54 GMACs and 4.88M params** matches PaSST-S's **0.471 mAP** at **~1/237th the
MACs** (EfficientAT's own DeepSpeed-profiled figure: 540M MACs for mn10 against 128B for PaSST).
AudioMAE's 86M parameters buy **+0.002 mAP over `mn10_as`** and cost a **CC BY-NC 4.0** licence
that the fleet cannot take — the same refusal §6.3 already made for BirdNET's weights, for the
same reason.

And the decisive argument is not compute at all, it is the one §8 records: **AudioSet-trained
embeddings lose to bird-trained embeddings on all six bioacoustic datasets tested.** Perch 2.0's
own Table 7 is the mechanism — coarsen the supervision and the embedding degrades monotonically;
AudioSet's 527 classes are the coarse end of that axis. CAV-MAE's audio-visual +0.046 mAP is
unreachable regardless: there is no video anywhere in this fleet.

**Where an MAE would earn its place, and only there:** as the *semantic encoder inside a codec*
(§6.3, SemantiCodec uses a k-means-discretized AudioMAE), not as the classifier or the backbone.

---

## 6. Neural audio codecs for edge spooling

### 6.1 The codec table

| codec | SR | **reconstructed bandwidth** | bitrates | frame rate | RVQ | params | encoder RTF | licence | weights |
|---|---:|---:|---|---:|---|---:|---|---|---|
| **EnCodec 24 kHz** | 24 kHz | **12 kHz** | **1.5 / 3 / 6 / 12 / 24 kbps** | 75 Hz | ≤32 × 1024 (10 bit) | **23.3M** (HF metadata) | **9.8×** 1-core laptop CPU @6 kbps (1.6× with entropy coding) | code **MIT**; ⚠️weights licence **unstated** in repo | HF `facebook/encodec_24khz` |
| EnCodec 48 kHz | 48 kHz | 24 kHz | 3 / 6 / 12 / 24 kbps | 150 Hz | ≤16 × 1024 | 19.1M | 6.8× | HF tag **mit** | HF `facebook/encodec_48khz` |
| **DAC 44.1 kHz** | 44.1 kHz | **22.05 kHz** | **1.78 / 2.67 / 5.33 / 8 kbps** | **86 Hz** | 9 × 10 bit @8 kbps | **76M** (22M enc / 54M dec) | ⚠️**not published** | **MIT, explicitly including weights** | GitHub, auto-download |
| DAC 16 / 24 kHz | 16 / 24 kHz | 8 / 12 kHz | as released | — | — | — | ⚠️not published | MIT | GitHub |
| SoundStream | 24 kHz | 12 kHz | 3–18 kbps (one model) | 75 Hz | 8 × 1024 @6 kbps | **8.4M** | **2.4× on one Pixel 4 CPU thread** | — | ⚠️**no official open weights** |
| Lyra v2 | 32 kHz | ~16 kHz | 3.2–9.2 kbps | 50 Hz | SoundStream backend | ⚠️not published | 27.4× enc / 67.2× dec | Apache-2.0 | GitHub |
| SemantiCodec | — | — | **0.31–1.40 kbps** (25/50/100 tok/s) | 25–100 Hz | k-means over a frozen AudioMAE + acoustic encoder, **diffusion decoder** | ⚠️not verified | ⚠️not verified | ⚠️not verified | GitHub |
| Opus *(classical baseline)* | 8–48 kHz | **4 kHz @8 kbps**, 16 kHz @14 kbps | 6–510 kbps | 2.5–60 ms frames | — | — | real-time, fixed-point available | BSD (RFC 6716) | everywhere |

### 6.2 Published quality — and the metric that does not apply

DAC's Table 3, 44.1 kHz, the one apples-to-apples low-rate table that exists:

| system | bitrate | mel dist ↓ | STFT dist ↓ | ViSQOL ↑ | SI-SDR ↑ |
|---|---:|---:|---:|---:|---:|
| **DAC** | **1.78 kbps** | 1.39 | 1.95 | **3.76** | 2.16 |
| DAC | 2.67 kbps | 1.28 | 1.85 | 3.90 | 4.41 |
| DAC | 5.33 kbps | 1.07 | 1.69 | 4.09 | 8.13 |
| DAC | 8 kbps | 0.93 | 1.60 | 4.18 | 10.75 |
| EnCodec (12 kHz BW) | 1.5 kbps | 2.11 | — | 2.82 | −0.02 |
| EnCodec | 6 kbps | 1.83 | — | 3.05 | 5.99 |
| Lyra (8 kHz BW) | 9.2 kbps | 2.71 | — | 2.19 | **−14.52** |
| Opus (4 kHz BW) | 8 kbps | 3.60 | — | 2.06 | 5.68 |

Retrained in EnCodec's own 24 kHz configuration, **DAC beats EnCodec at every rate** — ViSQOL
4.04 vs 3.98 at 1.5 kbps, 4.61 vs 4.42 at 24 kbps (Table 4).

⚠️**ViSQOL is a perceptual metric calibrated to human hearing.** For a bat call at 40 kHz or an
elephant rumble at 20 Hz it is not merely imprecise, it is measuring the wrong thing. Lyra's
**SI-SDR of −14.52** on general audio at 9.2 kbps is the warning: a speech-trained generative
codec can score acceptably on perceptual quality while destroying the waveform. Any
bioacoustic codec decision must be graded on a **task metric** (§7.5), not on ViSQOL.

### 6.3 What is actually known about codecs and bioacoustics: almost nothing

⚠️**No study evaluates EnCodec, DAC or SoundStream round-trip fidelity on bird, bat, whale or
insect audio.** Searched: arXiv (`"neural audio codec" AND bioacoustic`, `codec AND "bird song"`,
`BirdNET AND compression`) and OpenAlex (`bat call compression codec classification`,
`wildlife audio compression`). **No ViSQOL, SI-SDR or mel-distance figure exists for any neural
codec on any bioacoustic corpus.** That is the state of the literature, and it means any claim
that "EnCodec preserves spectrogram fidelity for bioacoustics" is currently unfounded in both
directions.

The three adjacent results that do exist:

- ⚠️**Against compression:** *"Audio data compression affects acoustic indices and reduces
  detections of birds by human listening and automated recognisers"*, *Bioacoustics* 2023,
  [doi:10.1080/09524622.2023.2290718](https://doi.org/10.1080/09524622.2023.2290718). MP3
  "decreased the number of detections" and produced "lower precision and recall for automated
  recognisers"; sample-rate reduction "introduced systematic bias to acoustic indices". The
  authors "recommend against the use of MP3 compression". ⚠️The abstract does not name BirdNET;
  treat "BirdNET was degraded" as unverified.
- ⚠️**For compression, if you embed:** *"How index selection, compression, and recording schedule
  impact the description of ecological soundscapes"*, *Ecology and Evolution* 2021,
  [doi:10.1002/ece3.8042](https://doi.org/10.1002/ece3.8042). Hand-crafted indices varied
  considerably under compression, but "the effects of this variation ... on the performance of
  classification models is minor", and a CNN AudioSet fingerprint was **12–16 % more accurate**
  and **tolerated CBR 64 kb/s → 8 % of file size "without any detectable effect"**. **This is the
  single most relevant published data point for a compress-then-embed pipeline**: the *learned*
  representation survived what the *hand-crafted* one did not.
- **The precedent for doing it properly:** ELP's AAAI 2019 paper frames the exact problem —
  "network bandwidth quickly becomes a bottleneck", and "most audio compression schemes are aimed
  at human listeners and are **unsuitable for low-frequency elephant calls**" — and answers it
  with an **end-to-end differentiable, species-adaptable compressor**. ⚠️Its bitrate/quality
  numbers were not extracted here.

**The structural fact that needs no study.** Reconstructed bandwidth is published for every codec
above. Bat echolocation (20–120 kHz) lies entirely outside every one of them. EnCodec 24 kHz's
12 kHz ceiling removes Perch's **12–16 kHz** mel bands before Perch runs. This is not an artifact
question, it is a Nyquist question, and it is decided before any experiment.

### 6.4 Which codec, for this fleet, and why

| requirement | consequence |
|---|---|
| must cover Perch's 16 kHz mel top | ⇒ reconstructed bandwidth ≥16 kHz ⇒ **EnCodec 24 kHz and all sub-24 kHz DAC variants are excluded** |
| must have a published operating point under ~3 kbps | ⇒ EnCodec 48 kHz (min 3 kbps, music-trained, **1 s latency**, stereo, non-causal) is marginal; **DAC 44.1 kHz at 1.78 / 2.67 kbps** qualifies |
| weights licence must survive `docs/data-governance.md` and the §6.3 precedent | ⇒ **DAC is MIT and says so for the weights**; EnCodec's weights licence is unstated in-repo (⚠️the common "CC-BY-NC" claim appears to be a conflation with AudioCraft/MusicGen — **treat as unverified in both directions**); SoundStream has no open weights at all |
| encoder must run somewhere that exists | ⇒ **22M parameters is not an ESP32-S3 workload.** Not close. |

**Conclusion: DAC 44.1 kHz at 1.78–2.67 kbps is the only published configuration that satisfies
band, bitrate and licence simultaneously** — and it cannot run on the xiao nodes. Its encoder
would have to live on a Pi-class device (hugbot is the only such node, and hugbot is capped at
4,501 Hz and already thermally constrained at 84 °C with a `CPUQuota` on every audio unit — see
§7 of the acoustic stack). ⚠️**DAC's encoder RTF on any CPU is NOT PUBLISHED.** SoundStream is
the only codec in the table with a published phone-CPU figure (2.4× real time at 8.4M params on
one Pixel 4 thread) and it is the one with no weights. That is the shape of the gap.

---

## 7. Edge spooling arithmetic, against this fleet's radio

### 7.1 What a 5 s clip costs, by representation

| representation | bytes per 5 s event | ratio to the WAV |
|---|---:|---:|
| **48 kHz 16-bit WAV** *(today, over WiFi only)* | **480,044** | 1× |
| Opus @14 kbps (16 kHz BW) | 8,750 | 55× |
| DAC 44.1 kHz @ 2.67 kbps | 1,669 | 288× |
| **DAC 44.1 kHz @ 1.78 kbps** | **1,113** | **431×** |
| EnCodec 24 kHz @ 1.5 kbps *(band-excluded, §6.4)* | 938 | 512× |
| SemantiCodec @ 0.31 kbps *(unverified)* | 194 | 2,474× |
| **Perch 1536-d float32 embedding** | **6,144** | 78× |
| Perch 1536-d int8 + scale | 1,540 | 312× |
| **172 B sketch** *(today, over the mesh)* | **172** | **2,791×** |

**The embedding is not a compression format.** A float32 Perch embedding is **5.5× larger than
the DAC-coded audio that produced it**, and 36× larger than the sketch. Quantised to int8 it
merely ties the codec. Whatever else embeddings are for, they are not a way to save uplink.

### 7.2 Meshtastic airtime, computed

Standard LoRa airtime formula, ShortTurbo (SF7 / BW 500 kHz / CR 4/5), **preamble 16 symbols**
(what Meshtastic ships), CRC on, explicit header — the same parameters
`docs/esp32s3-lora-node.md` §7.2 uses, which reproduces its 78.9 ms at 193 B on-air.

For a full 237 B Meshtastic payload + 16 B header + protobuf wrapper ≈ **257 B on-air**:
`Tsym = 128/500000 = 0.256 ms`; preamble `(16+4.25)·Tsym = 5.18 ms`;
payload symbols `8 + ceil((8·257 − 4·7 + 28 + 16)/28)·5 = 8 + 74·5 = 378` → `96.8 ms`;
**total ≈ 102 ms per 237 B**. That is **~2,325 B/s of payload at 100 % channel occupancy.**

| payload to ship | packets | airtime | at a fleet-wide **1 %** channel budget (36 s/h) |
|---|---:|---:|---|
| one 172 B sketch *(193 B on-air)* | 1 | **78.9 ms** | 456/h fleet-wide |
| DAC @1.78 kbps, 5 s (1,113 B) | 5 | **~0.51 s** | **~70 clips/h, fleet-wide** |
| EnCodec @1.5 kbps, 5 s (938 B) | 4 | ~0.41 s | ~88/h |
| Perch float32 embedding (6,144 B) | 26 | **~2.65 s** | ~13/h |
| **one 48 kHz WAV (480,044 B)** | **2,026** | **~207 s (3.4 min)** | **0.17/h — i.e. never** |

**Read that against the measured event rate.** At ~118 detections/h/node over 6 nodes (708
events/h), codec-spooling **every** event costs 708 × 0.51 s ≈ **361 s/h ≈ 10 % channel
utilisation** — on top of the 1.53 % the sketches already cost, before Meshtastic's rebroadcast
multiplies it (`docs/esp32s3-lora-node.md`: "the 1.5 % figure is a floor, not a prediction").

**Therefore: neural-codec spooling over the mesh is an event-*selected* transport, not a
continuous one.** Roughly **70 five-second clips per hour across the whole fleet** at a 1 %
budget — about **10 % of detections**. Which 10 % is a selection problem, and the fleet already
has the selector: the 172 B sketch, whose entire documented job is "to decide **which four
seconds are worth 128 kB of WiFi**" (§8). The same sentence with "1 kB of LoRa" substituted is
the design. Codec spooling does not replace the sketch; **it gives the sketch's verdict somewhere
to escalate to when there is no WiFi.**

### 7.3 Satellite

Iridium SBD carries **340 B per mobile-originated message** (verified via Whytock et al.,
[doi:10.1101/2021.11.10.468078](https://doi.org/10.1101/2021.11.10.468078), a camera-trap
deployment that hit the same wall). Computed:

| representation | 340 B messages per 5 s event |
|---|---:|
| 172 B sketch | **1** (with 168 B spare) |
| SemantiCodec @0.31 kbps (⚠️unverified) | 1 |
| EnCodec @1.5 kbps | 3 |
| **DAC @1.78 kbps** | **4** |
| Perch float32 embedding | 19 |
| 48 kHz WAV | **1,413** |

⚠️Iridium per-message cost is **NOT PUBLISHED** in any source verified here; Iridium's own SBD
page returned HTTP 403. Do not put a dollar figure in a capacity model without a fresh vendor
citation.

### 7.4 LoRaWAN, if the fleet ever leaves Meshtastic

Verified inputs: SF7/125 kHz/CR 4/5 = **5.5 kbit/s** raw; ETSI EN 300 220-2 duty cycles
(0.1 %/1 %/10 % by sub-band); TTN fair-use policy **30 s of uplink airtime per node per day**
(not a legal limit, a network policy). Computed from those: the fair-use ceiling is
`30 × 5500 = 165,000 bits/day ≈ 20.6 kB/day`, which at DAC 1.78 kbps is **~93 seconds of audio
per node per day** — or **18 five-second clips**. Under a pure 1 % duty cycle (864 s/day) the
physical ceiling is ~594 kB/day ≈ 45 min/day of coded audio, before framing overhead and the
51–222 B max application payload. ⚠️This arithmetic is derived here, not published; verify before
building a capacity model on it.

### 7.5 The experiment nobody has run, which this fleet can run

Because §6.3 found no published bioacoustic codec evaluation, the honest status of "codec
spooling preserves spectrogram fidelity" is **UNMEASURED**. It is also cheap to measure with
assets that already exist — the clip cache, `tools/hear_tag.py`, and the `mn10_as` ONNX graph
whose numerical agreement is already pinned to 1.0e-06 on embeddings:

1. Take the clip cache (`clips/index.jsonl`, 5 s 48 kHz WAVs with body SHA-256s).
2. Round-trip each clip through DAC 44.1 kHz at 1.78 / 2.67 / 5.33 / 8 kbps, and through Opus at
   14 / 24 kbps as the classical control. Resample 48 → 44.1 kHz on the way in and back out.
3. Score **task metrics, not ViSQOL**:
   - cosine distance between `mn10_as` **960-d embeddings** before and after;
   - rank agreement of the top-5 AudioSet tags, and the `Silence` top-1 rate that §6.2b already
     tracks;
   - per-mel-band log-magnitude error as a function of frequency — the number that answers
     "which part of the spectrum did the codec spend?";
   - once S1 exists, cosine distance between **Perch 1536-d** embeddings before and after, which
     is the metric that actually decides this.
4. **Pass condition, stated before the measurement:** a codec configuration is admissible only if
   the round-trip embedding-cosine shift is smaller than the spread the pipeline already tolerates
   between raw and normalised input. Otherwise the codec is changing the answer, and a spooled
   clip is not evidence about the same event.

This is a one-afternoon experiment that would produce the first published bioacoustic numbers for
a neural codec. It should precede any procurement, firmware or transport decision, and it
requires no node contact — the tagger/annotation boundary in `docs/ml-lifecycle.md` §1 holds.

---

## 8. Where each model would sit, if anywhere

| model | role here | verdict |
|---|---|---|
| **`mn10_as`** | coarse tagger, shipped | **keep.** 0.54 GMACs, MIT, 960-d, already hash-pinned |
| **Perch 2.0** | S1 embedding backbone | **keep as the choice.** Apache-2.0 both ends, 1536-d, 32 kHz from a 48 kHz decimation, best marine transfer measured. Open: first-party ONNX (§2.5), sm_75 gate, insect-head exclusion (§6.3) |
| Perch 1.0 | — | **no**, except as the RFCX-shaped reminder that 2.0 is not uniformly better |
| SurfPerch | — | **no.** 1280-d, reef-specialised, worse than Perch on all six novel domains |
| Google multispecies whale / humpback | — | **no.** No marine sensor exists in this fleet. Cited for the logits-vs-embeddings result |
| **BioLingual** | offline label bootstrapper against the clip cache | **maybe, off the data plane.** ⚠️Weights licence not published — resolve before any use, and never inside a shipped artifact |
| animal2vec | — | **no.** 8 kHz, 315M params, fairseq + Python ≤3.9 against a 3.12/3.13 CI matrix. Take the 1 %-labels finding, not the model |
| AudioMAE | — | **no.** CC BY-NC 4.0, 86M params, +0.002 mAP over `mn10_as` |
| CAV-MAE | — | **no.** No video exists in this fleet |
| PaSST / BEATs | — | **no**, unchanged from §8 of the acoustic stack. ~128 GMACs for parity with a 0.54 GMAC model |
| **DAC 44.1 kHz** | edge spool codec, event-selected | **the only candidate.** MIT incl. weights, 22.05 kHz band, 1.78 kbps published. Blocked on §7.5 and on a host that is not an ESP32-S3 |
| EnCodec 24 kHz | — | **band-excluded**: 12 kHz ceiling cuts Perch's own mel range |
| SoundStream | — | **no open weights.** Cited for the only published phone-CPU encoder RTF |
| SemantiCodec | — | **watch.** 0.31 kbps is the only thing that fits one Iridium message; ⚠️licence, params and RTF unverified, decoder is a diffusion model |

---

## 9. Corrections — claims this survey had to fix

Several of these are assertions made in the survey request or in circulation generally; two touch
this repository's own documents.

| claim | status |
|---|---|
| Perch 2.0 is a Conformer / ResNet | ❌ **EfficientNet-B3 CNN**, 12M params |
| Perch 2.0 trained on eBird / Macaulay / BioGeo | ❌ **Xeno-Canto, iNaturalist, Tierstimmenarchiv, FSD50K** |
| Perch 2.0 uses PCEN | ❌ **log-mel**; PCEN was Perch 1.x |
| Perch 2.0 covers bats | ❌ **bats deliberately removed from training** |
| Perch 2.0 at `kaggle.com/models/google/perch` | ❌ under `google/bird-vocalization-classifier`, instance `perch_v2` |
| Perch 2.0 is 1536-d, 32 kHz, 5 s, Apache-2.0, ~12M params | ✅ **all confirmed** — `docs/acoustic-stack.md` §6.3 is correct on every count |
| Perch 2.0 is SOTA on BirdSet | ⚠️ true on **AUROC**; **BirdMAE-L holds the best cmAP** (0.440 vs 0.431) |
| BioLingual's text encoder is GPT-2 | ❌ **RoBERTa** |
| BioLingual weights are openly licensed | ❌ **no licence published**; the Apache-2.0 is on the code repo only |
| Google's multispecies whale model is 10 kHz | ❌ **24 kHz**; 10 kHz is the humpback model |
| SurfPerch's paper is "Towards a General-Purpose Foundation Model..." | ❌ it is arXiv:2404.16436 / *Phil. Trans. R. Soc. B* 380:20240280 |
| "ElephantCallerID" | ❌ **does not exist** (0 GitHub results) |
| PaSST is MIT | ❌ **Apache-2.0** |
| PaSST ensemble 0.4965 | ⚠️ **0.4956** |
| BEATs 0.486 / 0.504 | ⚠️ **0.486 single / 0.506 ensemble** |
| EnCodec weights are CC-BY-NC | ⚠️ **unverified** — repo states MIT for code and is silent on weights; likely a conflation with AudioCraft/MusicGen |
| SemantiCodec floor is 0.35 kbps | ⚠️ published range is **0.31–1.40 kbps** |
| `mn10_as`: 4.88M / 0.54 GMACs / 0.471 mAP / 960-d / MIT / 32 kHz | ✅ **all confirmed** — §6.2b is correct |
| elephant rumble fundamental 14–35 Hz | ⚠️ peer-reviewed figure is **8–34 Hz** (Cornell AAAI 2019); 14–35 Hz is a third-party README |
| BirdNET model licence | ⚠️ **conflict**: README says CC BY-NC-**SA** 4.0, Zenodo metadata says `cc-by-nc-4.0`. §6.3's refusal stands either way |

---

## 10. Open numbers — what this survey could not close

| unmeasured | blocks | cost to close |
|---|---|---|
| does `tf2onnx` round-trip Perch 2.0 within `mn10_as`-grade tolerance | whether S1 can keep the §6.2b hash-pinned supply-chain rule | one export run and a numerical-agreement assertion |
| Perch 2.0 GMACs and per-clip latency on CPU (`perch_v2_cpu`) | whether §6.3's sm_75 gate matters at all | one container run over the staging clip set |
| 48 → 32 kHz resample penalty on Perch embeddings | nothing yet; it is the assumption behind "Gate 2 closed" | resample the same clips two ways, compare embeddings |
| **codec round-trip embedding shift on this fleet's own clips** | **every edge-spooling claim in §7** | §7.5 — one afternoon, no node contact |
| DAC encoder real-time factor on any CPU this fleet owns | whether codec spooling has a host at all | one timed encode on the Pi-class node |
| BioLingual weights licence | whether the label-bootstrap use is permissible | one upstream question |
| Iridium SBD per-message cost | any satellite capacity model | one vendor quote |
| hugbot thermal headroom for a 22M-parameter encoder | whether hugbot can be the codec host | one timed encode under its existing `CPUQuota` |

---

## 11. Sources

- Perch 2.0 — [arXiv:2508.04665](https://arxiv.org/abs/2508.04665); <https://www.kaggle.com/models/google/bird-vocalization-classifier>; `google-research/perch`, `google-research/perch-hoplite` (Apache-2.0)
- SurfPerch — [arXiv:2404.16436](https://arxiv.org/abs/2404.16436); [doi:10.1098/rstb.2024.0280](https://doi.org/10.1098/rstb.2024.0280); <https://www.kaggle.com/models/google/surfperch>
- BioLingual — [arXiv:2308.04978](https://arxiv.org/abs/2308.04978); <https://huggingface.co/davidrrobinson/BioLingual>; `david-rx/BioLingual`
- animal2vec / MeerKAT — [arXiv:2406.01253](https://arxiv.org/abs/2406.01253); *Methods Ecol. Evol.* 2026, [doi:10.1111/2041-210x.70218](https://doi.org/10.1111/2041-210x.70218); `livingingroups/animal2vec` (MIT); weights [doi:10.17617/3.ETPUKU](https://doi.org/10.17617/3.ETPUKU)
- Whale models — <https://www.kaggle.com/models/google/multispecies-whale>; <https://www.kaggle.com/models/google/humpback-whale>; Allen et al., *Front. Mar. Sci.* 2021, [doi:10.3389/fmars.2021.607321](https://doi.org/10.3389/fmars.2021.607321)
- Elephant infrasound — Bjorck et al., AAAI 2019, [arXiv:1902.09069](https://arxiv.org/abs/1902.09069); Keen et al., *JASA* 141(4) 2017, [doi:10.1121/1.4979476](https://doi.org/10.1121/1.4979476)
- DeepSqueak — `DrCoffey/DeepSqueak` (BSD-3); Coffey et al., *Neuropsychopharmacology* 44:859–868, [doi:10.1038/s41386-018-0303-6](https://doi.org/10.1038/s41386-018-0303-6)
- AudioMAE — [arXiv:2207.06405](https://arxiv.org/abs/2207.06405); `facebookresearch/AudioMAE` (CC BY-NC 4.0)
- CAV-MAE — [arXiv:2210.07839](https://arxiv.org/abs/2210.07839); `YuanGongND/cav-mae` (BSD-2)
- EfficientAT — `fschmid56/EfficientAT` (MIT); [arXiv:2211.04772](https://arxiv.org/abs/2211.04772), [arXiv:2310.15648](https://arxiv.org/abs/2310.15648), [arXiv:2303.01879](https://arxiv.org/abs/2303.01879)
- PaSST — [arXiv:2110.05069](https://arxiv.org/abs/2110.05069); `kkoutini/PaSST` (Apache-2.0). BEATs — [arXiv:2212.09058](https://arxiv.org/abs/2212.09058); `microsoft/unilm` (MIT)
- EnCodec — [arXiv:2210.13438](https://arxiv.org/abs/2210.13438); `facebookresearch/encodec`. DAC — [arXiv:2306.06546](https://arxiv.org/abs/2306.06546); `descriptinc/descript-audio-codec` (MIT). SoundStream — [arXiv:2107.03312](https://arxiv.org/abs/2107.03312). Lyra — `google/lyra`. SemantiCodec — [arXiv:2405.00233](https://arxiv.org/abs/2405.00233). Opus — RFC 6716
- Compression and bioacoustics — *Bioacoustics* 2023, [doi:10.1080/09524622.2023.2290718](https://doi.org/10.1080/09524622.2023.2290718); *Ecology and Evolution* 2021, [doi:10.1002/ece3.8042](https://doi.org/10.1002/ece3.8042)
- Link budgets — The Things Network LoRaWAN spreading-factor and duty-cycle documentation; ETSI EN 300 220-2 V3.2.1 §4.3.3; Whytock et al., [doi:10.1101/2021.11.10.468078](https://doi.org/10.1101/2021.11.10.468078) (Iridium SBD 340 B)
- In-tree — `docs/acoustic-stack.md`, `docs/acoustic-models-bleeding-edge.md`, `docs/uplink.md`, `docs/esp32s3-lora-node.md`, `docs/ml-lifecycle.md`, `docs/phase3-storage-capacity-model.md`, `docs/clip-pipeline.md`
