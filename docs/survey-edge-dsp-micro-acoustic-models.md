# Survey: micro-acoustic and edge DSP models for resource-constrained nodes

**Status: survey. Nothing here is adopted, ordered, flashed or budgeted.** This document
compares candidate techniques for the tier-1 node against the pipeline the fleet *already*
runs, and states for each candidate what it would cost on silicon this project owns or could
plausibly buy. It selects nothing. Where a candidate is attractive and would still be refused
today, the refusal and its reason are written down, because otherwise the same candidate will
be re-proposed next quarter with the same enthusiasm and no new evidence.

Audience: whoever is asked "why don't the nodes just run a small neural net?" and needs the
arithmetic rather than an opinion — and whoever eventually builds a microphone array and has
to decide whether an FPGA sits between the capsules and the host.

## How to read a number in this document

Every quantitative claim carries its provenance. There are five kinds and they are not
interchangeable:

| tag | meaning |
|---|---|
| **MEASURED** | measured in this tree or on this fleet, with the `path:line` or command that produced it |
| **DERIVED** | arithmetic performed in this document from MEASURED or DATASHEET inputs; the expression is shown so it can be rechecked |
| **DATASHEET** | a vendor part specification. True of the part, not of any board here |
| **VENDOR-CLAIM** | a vendor's own benchmark of their own software. Useful for ordering candidates, worthless as an acceptance criterion |
| **UNVERIFIED** | published or widely repeated, not checked here, and not to be planned against |

⚠️**No candidate in sections 1–4 has been run on any node in this fleet.** Every per-node
figure for a candidate is DERIVED or VENDOR-CLAIM. The only MEASURED per-node figures in this
document belong to section 0, which is the incumbent.

---

## 0. The incumbent: what a tier-1 node already does per second

A candidate is not compared against an empty MCU. It is compared against a node that is
already acquiring, DC-blocking, decimating, gating, sketching, ring-buffering, writing WAV
clips, disciplining a clock against PPS and serving HTTP — on two cores it also shares with
Wi-Fi. This section is the budget any new stage has to fit inside.

### 0.1 The signal path as built

Source: `firmware/hear_node/hear_node.ino`, `firmware/gen_decim.py`, `firmware/gen_mel.py`,
`firmware/gen_mel_scene.py`.

```
PDM mic ──I2S/PDM──> 48 kHz int16 ──DC block (1.6 Hz, 1 pole)──> acblk
                                        │
                                        ├──> aring (16384 samples, PSRAM-backed raw ring)
                                        ├──> sketch bank MELIMP_*  (NFFT 256, hop 192, 20 bands, 8 frames, @48 kHz)
                                        │
                                   449-tap FIR /3
                                        │
                                        v
                                  16 kHz int16 ──> envelope gate (16-sample mean |s|)
                                               └─> scene bank MELS_*  (NFFT 256, 20 bands, 4 slices/1.024 s, @16 kHz)
```

| stage | parameter | value | provenance |
|---|---|---|---|
| acquisition | `FS_ACQ = FS_NOMINAL * DECIM` | 48 000 Hz | MEASURED — `hear_node.ino:344` |
| PDM clock ceiling | mic Standard Performance Mode ≤ 4.0 MHz, ESP32 drives `fs*64` in `I2S_PDM_DSR_8S` | fs ≤ 62.5 kHz | DATASHEET — MSM261D3526H1CPM V1.2, cited at `hear_node.ino:337-342` |
| I2S DMA | `dma_desc_num 6 × dma_frame_num 240` | 1440 samples = **30.0 ms** of slack | MEASURED — `hear_node.ino:2428-2429` |
| block | `ABLOCK = BLOCK * DECIM = 768` | **16.0 ms** per read | DERIVED — 768/48000 |
| DC block | one pole, τ = 0.1 s | corner ≈ 1.6 Hz | MEASURED — `hear_node.ino:1697-1699` |
| decimator | 449 taps, /3, int16 | 7.18 M MAC/s ≈ **3 % of one core** | MEASURED (taps, folds) — `firmware/gen_decim.py`; DERIVED (rate) — 449 × 16000 |
| decimator quality | passband ripple ≤ 0.08 dB, worst in-band fold **−63.4 dB**, group delay 224 acq samples = 4.667 ms | MEASURED on the **quantised** taps — `gen_decim.py` docstring |
| raw ring | `ARING 16384` at `FS_ACQ` | **341.3 ms** | DERIVED — 16384/48000, constant at `hear_node.ino:1525` |
| gate | 16-sample moving mean of \|int16\| at 16 kHz | **1.0 ms** window | DERIVED — 16/16000, `hear_node.ino:1614-1615` |
| gate threshold | `max(g_amb × 8.0, g_floor)`, floor default 800, runtime-settable, `REARM 0.35` | — | MEASURED — `hear_node.ino:1616, 1640-1641` |
| gate adaptation | τ_rise 30 s, τ_fall 5 s (slow up, quick down) | — | MEASURED — `hear_node.ino:1682-1685` |
| sketch | 256-pt FFT, hop 192, 20 mel bands × 8 frames, span 1600 samples = **33.3 ms** | 172 B on the wire | MEASURED — `mel_impulse.h`, `hear/wire.py` profile 0 |
| scene | 256-pt FFT at 16 kHz, non-overlapping, 20 bands × 4 slices per 1.024 s | 62.5 FFT/s ≈ **0.64 MFLOP/s** | DERIVED — 5·N·log₂N·62.5 with N=256 |
| clip | 1.0 s pre + 4.0 s post at `FS_ACQ` | 480 000 B per event | DERIVED — 5 × 48000 × 2, `hear_node.ino:2540-2541` |
| PSRAM raw ring, largest tier | 80 s × `FS_ACQ` × 2 B | 7.68 MB of 8.34 MB | MEASURED — `hear_node.ino:2020` |

### 0.2 What that leaves

The continuous DSP load on the node today is **≈ 3 % of one core for the decimator plus
≈ 0.64 MFLOP/s for the scene bank**; the sketch is event-driven (8 FFTs per detection, not
per second). Everything else on the node — Wi-Fi, HTTP, SD, GPS parsing, clip writing — is
I/O-bound rather than MAC-bound.

⚠️**The binding constraint on this node is not MACs. It is the 30.0 ms DMA and the 16.0 ms
block.** `hear_node.ino:2439-2440` is a `static_assert` that one chunk's card read plus one
block must fit inside the DMA. Any new stage that runs *inline* with `audio_pump()` and takes
longer than ~14 ms per block does not slow the node down: it **drops audio**, and a dropped
block is a lost detection with no row anywhere that says so. That is the acceptance criterion
every candidate below is measured against, and it is a latency criterion, not a throughput one.

### 0.3 The classifier that exists

`model_gunshot_48k.json` is `gunshot_logmel_logistic_v1`: a **logistic regression on 2560
log-mel features** (40 mels × 64 frames, NFFT 512, hop 240, 20 Hz–20 kHz, 48 kHz), trained on
4296 examples (`modules/gunshot/train_gunshot.py`). It reports `accuracy 1.0` at
`positive_rate 0.5` — which is a statement about that training set and nothing else, and is
exactly the shape of result that section 3.5 exists to distrust.

Inference cost is a 2560-element dot product: **2560 MAC per decision**, ~10 KB of float32
weights, or **2.5 KB as INT8**. DERIVED. This is the floor any neural candidate must beat on
*accuracy per byte*, and it is a very cheap floor to have to beat.

`modules/gunshot/train_gunshot.py:115-121` already has a `--tflite` export path, guarded by an
optional TensorFlow import. Nothing consumes its output. That is the one existing hook into
the TFLM world in this tree.

---

## 1. Micro-VAD and TinyML voice triggers

### 1.1 What the class is

Sub-100 KB INT8 models that answer one binary question per frame — *is this speech / is this
the wake word* — cheaply enough to run continuously so that something expensive downstream can
stay asleep. Three implementation families are relevant to the silicon this project uses.

| framework | target | model format | quantisation | notes |
|---|---|---|---|---|
| **TFLite Micro (TFLM)** | any Cortex-M, Xtensa, RISC-V | `.tflite` FlatBuffer | INT8 / INT16 post-training | reference kernels are portable and slow; speed comes from the *kernel backend*, not TFLM |
| **CMSIS-NN** | Cortex-M4/M7/M33/M55 | TFLM kernel backend | INT8 (SIMD `SMLAD`), INT4 on newer | the actual reason a Cortex-M KWS model is fast |
| **ESP-DL + ESP-PPQ** | ESP32 / S3 / P4 | `.espdl` FlatBuffer | **w8a8, w16a16, w8a16 mixed** | zero-copy deserialisation, static memory planner, dual-core scheduling for Conv2D/DepthwiseConv2D | VENDOR — `github.com/espressif/esp-dl` README |

ESP-DL's own cross-target operator benchmark (v3.3.10, 955 common cases, geometric mean,
ESP32 = 1.00×) gives the ESP32-S3 PIE vector unit's contribution directly:

| operator | S3 speedup vs ESP32 | why it matters here |
|---|---|---|
| `Conv` | **22.9×** (3.40–171) | every CNN-shaped acoustic model |
| `ConvTranspose` | 31.8× | not used by classifiers |
| `MaxPool` | 37.1× | pooling stages |
| `Gemm` | 2.36× | **the dense layer — barely accelerated** |
| `MatMul` | 3.04× | same |
| `LSTM` / `GRU` | 3.92× / 3.80× | recurrent VADs get much less than convolutional ones |

VENDOR-CLAIM — `esp-dl/benchmark_report.md`, generated 2026-09-01. Not reproduced here.

⚠️**The 22.9× is a convolution number and does not generalise.** A candidate whose cost is
dominated by dense layers or recurrence gets 2.4–3.9×, not 22.9×. `gunshot_logmel_logistic_v1`
is a single `Gemm`: porting it to ESP-DL would buy **2.36×** on a 2560-MAC operation that is
already free. Do not port it.

### 1.2 Espressif ESP-SR: the closest thing to a drop-in on hardware this fleet owns

ESP-SR ships `WakeNet` (wake word), `MultiNet` (command words) and `VADNet` (voice activity)
as pre-trained, pre-quantised models with S3-specific kernels.

- **WakeNet9/9l**: dilated-convolution structure, supports ESP32, ESP32-S3, ESP32-P4.
  **WakeNet9s**: depthwise-separable, for C3/C5/C6. VENDOR — esp-sr wake word engine docs.
- **WakeNet10, 3-channel, `DET_MODE_3CH_90`: 22.6 % of one core.** VENDOR-CLAIM — esp-sr
  benchmark page. This is the single most useful external number in this section, because it
  is the same chip family the fleet runs and it is expressed in the same unit as the 3 % the
  decimator costs.
- **VADNet** has a documented 1–3 frame intrinsic trigger delay plus a `vad_min_speech_ms`
  hold, which is why AFE v2.0 carries a `vad_cache` to recover the truncated onset. VENDOR —
  esp-sr VADNet docs.

⚠️**That trigger delay is disqualifying for this application, and the reason is physical, not
architectural.** A VAD exists to answer "has speech started, roughly". This fleet exists to
answer "at what UTC instant did a wavefront reach *this* capsule", and
`docs/node-hardware.md` fixes the budget: 1 °C of temperature error is **183 µs over 35 m**,
and the firmware back-dates every detection by the decimator's 4.667 ms group delay
(`hear_node.ino:5493-5495`) rather than accept it. A detector with a 1–3 frame (10–30 ms)
onset ambiguity is **50–150× the error the clock discipline was built to eliminate**. It can
say *what* a sound was. It must never be allowed to say *when*.

**This is the central finding of sections 1 and 3, and it decomposes the problem:**

> A neural trigger may **classify**. The existing energy gate must keep **timing**. The two
> are different outputs of the same event and must not be merged into one model, because
> the one that is easy to improve is the one whose errors are cheap.

### 1.3 Footprint arithmetic

Sub-100 KB INT8 is not a marketing bound; it is roughly what fits alongside this firmware.
DERIVED budget for a XIAO ESP32-S3 Sense node as built:

| resource | total | already committed | plausibly free for a model |
|---|---|---|---|
| internal SRAM | 512 KB (DATASHEET) | firmware + Wi-Fi + lwIP + FreeRTOS + `stream_buf` + FFT scratch | **tens of KB, not hundreds** — UNVERIFIED, never measured |
| PSRAM | 8.34 MB MEASURED (`hear_node.ino:2020`) | 7.68 MB raw ring at the largest tier; 2.88 MB at the 30 s tier | 0.66 MB at top tier, ~5.4 MB at the 30 s tier |
| flash | 8 MB typical, partitioned for OTA A/B | two app slots + SPIFFS | a model must fit **twice**, once per OTA slot |

⚠️**PSRAM weights are not free weights.** PSRAM on the S3 is reached over an octal/quad SPI
bus and is *far* slower than SRAM; a model whose weights live in PSRAM and are streamed per
inference will not hit any vendor latency figure, all of which assume internal RAM. ESP-DL's
static memory planner exists precisely to place layers by a user-specified internal-RAM
budget. UNVERIFIED here — no S3 PSRAM inference has been timed in this tree.

⚠️**And the raw ring is already spending the PSRAM.** `hear_node.ino:2041` records that the
30 s tier exists to fit small-PSRAM parts. A model that demands 3 MB of PSRAM is not competing
with free space; it is competing with the pre-trigger audio that is the only artefact able to
say what a sound was after the fact. That trade is a *product* decision, not a memory one.

### 1.4 Edge Impulse

Edge Impulse is a pipeline (DSP block → model → deployment SDK), not a model. Its relevance
here is the DSP blocks, which are the same log-mel/MFE families this tree already implements
by hand: MFE, MFCC, spectrogram, and target-specific variants — the Syntiant block, for
instance, is fixed at 16 kHz and applies a proprietary pre-emphasis + log-Mel-filterbank
extractor for NDP101/NDP120 parts. VENDOR — Edge Impulse Studio processing-block docs.

**Assessment: not a candidate for this fleet, and the reason is the corpus, not the tool.**
`docs/ml-lifecycle.md` requires reproducible in-tree training; `firmware/gen_mel.py` and
`gen_golden.py` produce a filterbank checked **byte-exact** against golden vectors by
`firmware/hear_poc`, and `hear/wire.py` profile 0 *is* the 20×8 shape already on the wire for
228k rows. A hosted extractor whose coefficients this tree cannot regenerate breaks the one
property — byte-exactness of the band axis — that makes historical rows comparable to new ones.
Adopting it would be a decision to abandon the corpus, and should be written as one if it is
ever made.

### 1.5 Verdict on section 1

| candidate | fit | why |
|---|---|---|
| ESP-SR WakeNet/VADNet | **refuse for timing, possible for triage** | 22.6 % of a core is affordable; the 1–3 frame onset delay is not, and speech is not the target class |
| TFLM + ESP-DL custom AED head | **the real candidate** — see §3.6 | `Conv` at 22.9× is the only large speedup on offer, so the model must be convolutional |
| TFLM + CMSIS-NN | **relevant only if a Cortex-M node is ever built** | no M4/M7 node exists in this tree today |
| Edge Impulse | **refuse** | non-reproducible band axis breaks the frozen wire profile and the 228k-row corpus |

---

## 2. On-chip acoustic feature extraction

### 2.1 FFT

The node runs its own radix-2 256-point FFT with a precomputed twiddle table in float32
(`hear_node.ino:1534-1559`). It is not assembly, not SIMD, and not from `esp-dsp`.

| path | cost per 256-pt FFT | provenance |
|---|---|---|
| in-tree scalar float FFT | ~10 240 flops (5·N·log₂N) | DERIVED |
| `esp-dsp` `dsps_fft2r_fc32_ae32` | hand-written Xtensa assembly, same algorithm | VENDOR |
| ESP32-S3 PIE 128-bit SIMD int16 | 4–8 lanes per op on int16 | DATASHEET (the ISA); UNVERIFIED (the gain on *this* workload) |

At 62.5 FFT/s the scene lane spends **0.64 MFLOP/s** — DERIVED, ~0.3 % of one core at a
generous 1 flop/cycle at 240 MHz. **There is no FFT problem to solve on this node.** Swapping
to `esp-dsp` would optimise something that is already three orders of magnitude inside budget,
while introducing a dependency whose numerical output must then be re-proved byte-exact
against `firmware/hear_poc`'s golden vectors. Refuse until the FFT rate rises by ~100×.

The FFT rate *does* rise by ~100× under two of the candidates below (§3.4 continuous
spectral flux at 100 fps, §4.5 GCC-PHAT at 1024 points per pair). If either is adopted, the
`esp-dsp` question reopens with an actual motive.

### 2.2 Mel filterbanks

Both banks are **generated tables**, not runtime computation: `gen_mel.py` →
`mel_impulse.h` (48 kHz axis, 20 bands, NFFT 256) and `gen_mel_scene.py` → `mel_scene.h`
(16 kHz axis, same band count, spanning down to 62.5 Hz). Per-band cost is a sparse dot
product over the bins that band covers — `mel_scene.h` shows bands of 4–22 bins.

Two properties of this design are worth stating because they are what a hardware
accelerator would have to preserve:

1. **The bank is the axis.** `gen_mel_scene.py` states it: *f_lo, f_hi, nfft, bands and layout
   together ARE the band axis; change any of them and band k stops meaning what every scene
   row already on the card says it means.* An accelerator with fixed filterbank geometry is
   therefore not a drop-in — it is a corpus migration.
2. **The two banks deliberately differ.** The sketch bank reads the 48 kHz acquisition stream
   and the scene bank reads the 16 kHz decimated stream, each guarded by its own
   `static_assert` (`hear_node.ino:2205-2207`). Any accelerator must run *two* configurations
   concurrently, not one.

The window is shared: `np.hanning(nfft)` is a function of `nfft` alone, so `mel_scene.h`
emits no window and reuses `MELIMP_WIN`, saving 1024 B of flash. MEASURED — `mel_scene.h:7-10`.

### 2.3 I2S DMA double-buffering

The node's I2S DMA holds `6 × 240 = 1440` samples = **30.0 ms** at `FS_ACQ`, against a 16.0 ms
processing block. That is a 1.875× margin, and it is *asserted*, not assumed:

```
hear_node.ino:2436  static_assert(I2S_DMA_SAMPLES >= ABLOCK,        "the DMA must hold one whole block");
hear_node.ino:2439  static_assert(BLOCK_US + STREAM_CARD_US <= I2S_DMA_US,
                                  "one chunk read between two due blocks must fit in the DMA");
```

This is the most important structure in the firmware for anyone adding a stage, and it
generalises beyond this project: **on a streaming node, the DMA depth is the compute budget.**
The classic ESP32 failure mode — a model that "runs at 12 ms per inference, well under the
16 ms frame" and still drops audio — happens because inference latency is *jittery* (cache
misses, PSRAM stalls, Wi-Fi interrupts) while the DMA is fixed. A candidate needs headroom
against its **worst** case, not its mean, and this firmware would rather refuse a stage at
compile time than discover the drop in the field.

Recommended rule for any future stage, DERIVED from the asserts above:

> p99 stage latency + `STREAM_CARD_US` (4 ms) + `BLOCK_US` (16 ms) ≤ `I2S_DMA_US` (30 ms)
> ⇒ **a new inline stage has ≈ 10 ms of p99 budget per 16 ms block**, i.e. ~62 % duty on one
> core, and zero tolerance for a tail.

An off-block worker on the second core escapes this bound, at the price of a queue whose
depth then becomes the new drop point.

### 2.4 PDM decimation: software here, CIC on an FPGA

The node acquires PDM and decimates in software because the microphone's own decimator was
lost when the acquisition rate moved from 16 kHz to 48 kHz — `gen_decim.py` states the cost
explicitly: *running the PDM mic at 16 kHz let its internal decimator do the anti-aliasing;
acquiring at 32 kHz and decimating in software moves that job here, so this filter must not be
worse than the one it displaces or the scene corpus quietly degrades at the top of its band.*

The design record there is unusually complete and worth preserving as the comparison baseline:

- /3 from 48 kHz folds **both** the k=1 and k=2 images into the used band, so the stopband
  runs from 8187.5 Hz to Nyquist and **every** image was measured, not just the first.
- A 33-tap halfband was tried and **rejected at −7.7 dB worst-case fold**.
- The shipped 449-tap int16 filter measures **−63.4 dB** worst in-band fold, ≤ 0.08 dB
  passband ripple, 224 samples group delay.
- The measurement was taken on the **quantised** taps: *the float design reads better and the
  int16 filter that actually runs does not.*

The canonical FPGA alternative is a **CIC (Hogenauer) decimator followed by a compensation
FIR**: an N-stage integrator comb pair needs no multipliers at all — only adders and registers
— which is exactly why it is the standard PDM front end on parts without DSP blocks.

| property | software FIR on S3 (as built) | CIC + comp-FIR on iCE40 |
|---|---|---|
| multipliers | 449 MAC/output ≈ 7.18 M MAC/s | **zero** in the CIC; a short comp-FIR after |
| passband droop | none by design | sin(x)/x droop **must** be compensated, or the top of the band sags |
| bit growth | n/a | N·log₂(R·M) bits — a /3 at N=4 is modest, a /64 at N=5 is not |
| cost | ~3 % of one core MEASURED | LUTs + one BRAM; no CPU at all |
| proving it | `tests/test_decim_filter.py` exists | a new golden-vector harness would have to be built |

⚠️**Moving decimation to an FPGA is only worth it when the CPU is the bottleneck, and on this
node it is not — the decimator costs 3 %.** The real motive for a CIC front end is §4: when
there are 8–32 PDM capsules, 32 × 7.18 M = **230 M MAC/s** of decimation alone, which no ESP32
will do. Decimation moves to hardware because of *channel count*, never because of *taps*.

### 2.5 Device comparison for the feature-extraction job

| part | compute fabric | on-chip memory | relevance |
|---|---|---|---|
| **ESP32-S3** (Xtensa LX7 ×2 @240 MHz) | PIE 128-bit SIMD, single-precision FPU | 512 KB SRAM + up to 8 MB PSRAM | DATASHEET. The fleet's actual part |
| **iCE40 UP5K** | 5280 LUT4, **8 DSP blocks (16×16 MAC)**, 1 Mbit SPRAM + 120 kbit BRAM | ~128 KB SPRAM | DATASHEET. Enough for CIC chains and small filters; **8 multipliers is the hard ceiling** |
| **iCE40 HX8K / LP8K** | 7680 LUT4, **no DSP blocks**, 128 kbit BRAM | small | DATASHEET. Multiplier-free logic only — CIC yes, FIR bank no |
| **Zynq-7020** | 85K logic cells, **220 DSP48E1**, 4.9 Mbit BRAM, dual Cortex-A9 @667 MHz | large | DATASHEET. The only part in this list that can beamform 32 channels |
| **Cortex-M4F** | single-cycle MAC, 32-bit SIMD DSP ext | vendor-dependent | DATASHEET. CMSIS-DSP/CMSIS-NN target |
| **Cortex-M7** | dual-issue, caches, 2× DSP throughput of M4 | vendor-dependent | DATASHEET. Cache makes timing *less* predictable — relevant to §2.3 |

⚠️**The iCE40 UP5K's 8 DSP blocks are the number that decides its role.** Eight 16×16
multipliers at, say, 24 MHz is 192 M MAC/s *at best* and only with perfect utilisation. That
buys multiplier-free decimation and phase alignment for a modest array. It does not buy a
filterbank per channel, and it certainly does not buy inference. An iCE40 in this system is a
**router and decimator**, not a **classifier**.

---

## 3. Acoustic event detection on microcontrollers

### 3.1 The incumbent detector, stated as an algorithm

The node's gate is a **1 ms moving-average envelope with an adaptive threshold**:
`e > max(g_amb × 8, g_floor)`, where `g_amb` tracks the envelope with τ_rise 30 s and
τ_fall 5 s, and re-arms below 0.35 × threshold. MEASURED — `hear_node.ino:1703-1724`.

This is, structurally, an **STA/LTA detector** — the seismology standard — with STA = 1 ms
and LTA ≈ 30 s and a trigger ratio of 8. That it was arrived at independently is a point in
its favour: it is the right family for impulsive events.

The two design scars on it are the instructive part:

1. **The DC pedestal.** MEASURED: `mean(s) = 1285.8` against `mean(|s|) = 1285.3` — identical,
   which can only happen if the waveform never crosses zero. The envelope was measuring the
   PDM mic's offset, pinning the threshold at 8 × 1285 = 10280, so *a real event had to swing
   27 % of full scale to register*. `hear_node.ino:1690-1696`. **Any candidate in this section
   inherits this trap**: TKEO, spectral flux and every energy statistic are equally poisoned
   by a pedestal, and the DC block at 1.6 Hz is a precondition for all of them.
2. **Two sites computing one threshold.** The gate recomputed the threshold with a literal
   while `gate_thr()` computed its own, so a runtime floor change would leave `/status` and
   `health.csv` reporting a threshold the gate was not using. `hear_node.ino:1709-1713`. A
   record that looks correct and is not.

### 3.2 STA/LTA — the refinement available for free

Classical STA/LTA differs from the incumbent in one respect: it uses **energy** (s²) rather
than **|s|**, and typically a rectangular LTA rather than a one-pole EMA.

| variant | detector statistic | cost/sample | note |
|---|---|---|---|
| incumbent | mean\|s\| over 16, ratio to EMA | 1 add, 1 sub, 1 mul | MEASURED |
| energy STA/LTA | mean(s²) over N, ratio to long mean | +1 mul | DERIVED — sharper on impulses; squaring widens the crest-factor gap |
| recursive STA/LTA | both windows as EMAs | 2 mul, 2 add | DERIVED — O(1) memory, no ring |

DERIVED cost of adding energy STA/LTA alongside the incumbent at 16 kHz: **~2 extra
operations per sample = 32 k op/s, ≈ 0.01 % of a core.** It is free. What it is not is free of
consequence: a second detector that fires on different events changes the *corpus*, and every
existing row was gated by the incumbent.

### 3.3 Teager-Kaiser energy operator (TKEO)

Ψ[s(n)] = s(n)² − s(n−1)·s(n+1). Three multiplies and one subtract per sample, no state
beyond two samples.

- Tracks **instantaneous energy ∝ A²ω²**, so it weights *high-frequency* transients far more
  than an amplitude envelope does. That is precisely the discriminant for a gunshot's muzzle
  blast or a crack against wind and traffic — a wind gust is large-amplitude and *low*-frequency,
  so A² is big and ω² is small.
- Cost: **4 op/sample at 16 kHz = 64 k op/s, ≈ 0.03 % of a core.** DERIVED.
- Latency: **one sample** (needs s(n+1)). At 16 kHz that is 62.5 µs, which is *inside* the
  183 µs timing budget from `docs/node-hardware.md` — the only candidate in this entire
  document of which that is true.
- ⚠️Requires the DC block (§3.1) and is **extremely** sensitive to it: a pedestal makes
  s(n)² dominate and the operator degenerates.
- ⚠️Amplifies broadband noise along with transients; it is normally used as a *pre-emphasis*
  feeding a threshold, not as a detector on its own.

**This is the strongest single candidate in the document**: four operations per sample, sample
latency, physically motivated discrimination against the exact confusers this fleet has (wind,
traffic), and no effect on any existing band axis or wire profile.

### 3.4 Spectral flux

Σ_k max(0, |X_t(k)| − |X_{t−1}(k)|) — positive-part change in the magnitude spectrum, the
standard music-information-retrieval onset detector.

- The node **already computes the spectra it needs**: the scene lane produces 20 bands every
  16 ms. Band-domain flux over those is 20 subtracts, 20 max, 20 adds per slice = **60 op per
  16 ms = 3.75 k op/s**. Effectively free. DERIVED.
- ⚠️**Its latency is one hop.** Band-domain flux over the scene lane is a 16 ms detector, and
  §1.2's argument applies unchanged: it may classify, it must not time.
- ⚠️Full-resolution flux at a 10 ms hop with NFFT 512 costs ~2.3 MFLOP/s of FFT alone
  (DERIVED, 5·512·9·100) — ~4× the whole scene lane. Affordable, but this is the case that
  reopens §2.1.
- Where it earns its place: **flux separates a transient from a level shift**, which neither
  the envelope gate nor TKEO does. A door slam and a truck passing can produce the same
  envelope excursion and completely different flux.

### 3.5 Target classes: gunshot, chainsaw, motorcycle

These three are not one problem. They separate by **time scale**, and the time scale decides
the architecture:

| class | duration | signature | detector family | gated by the incumbent? |
|---|---|---|---|---|
| **gunshot** | muzzle blast < 3 ms; N-wave shorter still | impulsive, broadband, very high crest factor | TKEO / STA-LTA + a short classifier | **yes** — this is what the gate was built for |
| **chainsaw** | minutes | harmonic stack with a stable fundamental, amplitude-modulated by the cut | **spectral**, over seconds — the scene lane, not the gate | **no** — no onset to trigger on |
| **motorcycle** | seconds | harmonic, Doppler-swept, engine-order structure | spectral + tracking | **no** — same reason |

⚠️**Two of the three named target classes cannot be detected by the node's gate at all, and
this is the most actionable finding in section 3.** The gate is an onset detector. A chainsaw
running for four minutes produces *one* onset at most, and probably none if it fades in above
an ambient floor whose τ_rise is 30 s — the adaptive threshold will **track the chainsaw and
stop seeing it**. Sustained sources must be detected in the **scene** lane, which is already
capturing 20 bands × 4 slices every 1.024 s and is currently used for no detection whatsoever.

That is a large, cheap, already-paid-for opportunity: the scene descriptor is a continuous
20-band time-frequency record at 62.5 frames/s, it is already written to the card, and
**nothing classifies it on the node**. A harmonic-product-spectrum or band-ratio test over
scene slices costs on the order of tens of operations per second.

⚠️And the amplitude cue this rests on is partly gone before it starts: `docs/node-hardware.md`
records the ICS-43434's **AOP of 120 dB SPL** against a rifle report at 125–135 dB at 50 m,
with **15 % clipping already measured in the field**, and notes that *`ref_db` alone was worth
AUC 0.90*. Clipping is time-valid and amplitude-invalid. Any gunshot classifier trained on
un-clipped corpora will not transfer, and any candidate that leans on absolute level is
leaning on a cue this microphone destroys at exactly the range of interest.

And on `accuracy 1.0` (§0.3): a detector for events this rare is judged on **false alarms per
node-hour at a fixed recall**, never on accuracy at a balanced positive rate. The existing
model's headline number is not wrong, it is a different question's answer.

### 3.6 KWS architectures, read for what they say about AED

The Google Speech Commands lineage (DS-CNN, DNN, CNN, GRU at S/M/L sizes) is the reference
point for "how much model fits in an MCU", and the mapping onto this problem is direct:

| model | typical size | typical ops/inference | note |
|---|---|---|---|
| DNN-S | ~80 KB | ~0.08 M | dense — gets **2.36×** from S3 PIE, not 22.9× |
| DS-CNN-S | ~38 KB INT8 | ~2.7 M | depthwise-separable conv — **the shape ESP-DL accelerates**, and the one with dual-core scheduling |
| DS-CNN-L | ~500 KB | ~57 M | does not fit the budget in §1.3 |

UNVERIFIED — figures are the widely cited Hello-Edge/MLPerf-Tiny ranges, not re-measured here.

DERIVED duty cycle for DS-CNN-S on an S3, and this is the arithmetic that decides the design:

| inference rate | raw ops/s | at 1 op/cycle/240 MHz | with ESP-DL `Conv` 22.9× |
|---|---|---|---|
| 1 /s (event-gated) | 2.7 M | 1.1 % | negligible |
| 10 /s | 27 M | 11 % | ~0.5 % |
| 100 /s (continuous) | 270 M | **113 % — does not fit** | ~5 %, if and only if the whole model is conv |

**Conclusion: an event-gated convolutional classifier is comfortably affordable; a continuous
one is affordable only at the vendor speedup, which applies only to the convolutional part.**
Combined with §1.2, the architecture that falls out is not a choice so much as a consequence:

```
  energy gate (1 ms, MEASURED, keeps the UTC instant and the 4.667 ms group-delay correction)
        │ fires
        ├─ TKEO + flux confirmation  (§3.3/§3.4 — sample-latency, ~0.04 % of a core)
        │
        └─ DS-CNN-S over the 341 ms raw ring, ~1-10 inferences/s, INT8 via ESP-DL
                 │
                 └─> a LABEL and a CONFIDENCE in dets.csv. Never a timestamp.

  scene lane (62.5 fps, 20 bands, already written, currently unclassified)
        └─ sustained-source test for chainsaw/motorcycle — see §3.5
```

---

## 4. FPGA preprocessing for multi-microphone arrays

⚠️**This section is entirely prospective. There is no array, no FPGA and no multi-capsule
board in this repository** — `grep -ri "ice40\|zynq\|fpga\|beamform"` over the tree returns
nothing outside this document. Every number below is DERIVED from first principles or
DATASHEET, and none has been simulated, synthesised or measured.

### 4.1 Why an FPGA enters the picture at all

Not for MACs. For **pin count, channel count and determinism**.

| M capsules | raw PDM in | decimation to 16 kHz | I2S/TDM channels an ESP32 can take |
|---|---|---|---|
| 2 | 2 × 3.072 Mbit/s | 14.4 M MAC/s | fine today |
| 8 | 8 × 3.072 Mbit/s | 57 M MAC/s | **already past comfortable** |
| 16 | 16 | 115 M MAC/s | no |
| 32 | 32 | **230 M MAC/s** | no |

DERIVED — 449 taps × 16 000 outputs × M. The ESP32-S3 has a small fixed number of I2S
peripherals; 32 PDM capsules is not a software problem, it is a *pins and clocks* problem, and
that is what programmable logic is for.

### 4.2 Phase alignment is the whole reason the FPGA must be the clock domain

The system's founding constraint, from `docs/node-hardware.md`: **183 µs = 35 m of acoustic
path = 1 °C of temperature error**, and the fleet spends a GPS PPS per node to beat it.

Within an array, the requirement is far tighter. For an aperture d and sound speed c ≈ 343 m/s,
the maximum inter-capsule delay is d/c:

| aperture | max delay | samples at 16 kHz | samples at 48 kHz |
|---|---|---|---|
| 0.1 m | 292 µs | 4.7 | 14.0 |
| 0.5 m | 1.46 ms | 23.3 | 70.0 |
| 1.0 m | 2.92 ms | 46.7 | 140.0 |

DERIVED. **The entire signal of interest to a beamformer lives in single-digit-to-hundreds of
samples of relative delay.** Therefore:

⚠️**Every capsule must be sampled from one clock, and that clock must be the FPGA's.** Two
microphones on two I2S peripherals with independent DMA are not phase-coherent, and no
post-hoc alignment recovers what was never sampled coherently. This — not throughput — is the
argument that an FPGA (or one multi-channel TDM codec) is *mandatory* past two capsules, and
it should be recorded as such before anyone prototypes an array on two ESP32s.

Sub-sample alignment, when the geometry needs finer than one sample, is a fractional-delay
filter — a short Farrow/Lagrange interpolator per channel, a handful of multipliers each.
32 channels × 4 taps = 128 MAC per sample = **2.05 M MAC/s at 16 kHz** (DERIVED), well inside
even an iCE40 UP5K's 8 DSP blocks at modest clock. Doing it *before* decimation, at 48 kHz, is
3× that and still fits.

### 4.3 Delay-and-sum beamforming

Cost is trivially M multiply-accumulates per output sample per beam:

| M | 1 beam @48 kHz | 16 beams | 64 beams |
|---|---|---|---|
| 8 | 0.38 M MAC/s | 6.1 M | 24.6 M |
| 16 | 0.77 M | 12.3 M | 49.2 M |
| 32 | 1.54 M | 24.6 M | **98.3 M** |

DERIVED — M × 48000 × beams.

- **iCE40 UP5K** (8 DSP): comfortable to ~16 beams × 16 mics; 32×64 is out of reach.
- **Zynq-7020** (220 DSP48E1): 98.3 M MAC/s is a small fraction of its capability — a single
  DSP48E1 at 100 MHz is 100 M MAC/s, so 32 mics × 64 beams is **~1 DSP slice of arithmetic**
  and the real cost is memory bandwidth and delay-line storage, not multipliers.
- **Delay-line memory**, the term people forget: M × max_delay samples × 2 B. 32 × 140 × 2 =
  **8.96 KB** at 48 kHz for a 1 m aperture (DERIVED) — fits in iCE40 BRAM, trivial on Zynq.

Delay-and-sum is the right first beamformer precisely because it is **linear, deterministic,
and does not change what a sample means** — it is a sum of aligned copies. Adaptive
beamformers (MVDR, GSC) require covariance estimation and inversion, are data-dependent,
and would put a *non-deterministic, signal-dependent* transform upstream of a detector whose
timing is the product. Refuse adaptive beamforming on the FPGA for that reason alone, not for
a resource one.

### 4.4 Cross-correlation and GCC-PHAT

Pair count grows quadratically and this is the number that kills naive designs:

| M | pairs M(M−1)/2 |
|---|---|
| 8 | 28 |
| 16 | 120 |
| 32 | **496** |

Per-pair GCC-PHAT with a 1024-point FFT costs ~51 200 flops (DERIVED, 5·N·log₂N). At 100
frames/s: 28 pairs → 143 MFLOP/s; 496 pairs → **2.5 GFLOP/s**. The latter is a Zynq-class or
GPU-class number, not an iCE40 one.

The standard escapes, in the order they should be tried:
1. **Do not use all pairs.** A reference-channel scheme is M−1 correlations, not M(M−1)/2 —
   31 instead of 496 at M=32, a **16× reduction**, at the cost of robustness to a bad reference.
2. **Correlate on the decimated stream.** 16 kHz instead of 48 kHz is 3× fewer frames and a 3×
   coarser delay grid — then interpolate the peak parabolically for sub-sample resolution.
3. **Time-domain sliding correlation over a narrow lag window.** Only ±140 lags are physically
   possible for a 1 m aperture (§4.2); a full 1024-point FFT computes 1024 lags, of which
   **86 % are physically impossible**. A bounded time-domain correlator is 2·140 MAC per
   sample per pair and maps directly onto FPGA DSP blocks with no FFT, no bit-reversal and no
   complex arithmetic.

⚠️**Option 3 is the one an iCE40 can actually do, and it is a better fit than the textbook
answer.** It exploits a constraint the textbook does not have: this array's geometry is
*known* and *fixed*, so the lag search space is bounded by physics rather than by transform
length. DERIVED: 31 reference pairs × 280 lags × 16 000 = **139 M MAC/s** — at the edge of a
UP5K's 8 DSP blocks, comfortable on a Zynq, and it needs no FFT at all.

### 4.5 What crosses the boundary to the host

The point of the FPGA is to make the link *smaller* than the capsules:

| interface | payload | rate | provenance |
|---|---|---|---|
| raw PDM, 32 ch | 32 × 3.072 Mbit/s | **98.3 Mbit/s** | DERIVED — impossible over any ESP32 link |
| decimated PCM, 32 ch @16 kHz/16 bit | 32 × 256 kbit/s | 8.2 Mbit/s | DERIVED — needs TDM, still large |
| **4 beams @16 kHz/16 bit** | 4 × 256 kbit/s | **1.02 Mbit/s** | DERIVED — comfortable I2S/TDM |
| **delay estimates + 20-band sketch** | ~200 B/event | negligible | the shape this repo already uses |

**A 96× reduction from raw PDM to four beams.** That ratio is the whole architectural
argument, and it also says what the FPGA must *not* do: it must not be the thing that decides
an event happened, because then the host cannot audit it. It decimates, aligns, sums and
correlates — deterministic, auditable transforms — and the host keeps detection, timing and
classification. `docs/REDESIGN-LESSONS.md`'s recurring theme is that a silent, unauditable
stage is worse than a slow one.

### 4.6 Device verdict for the array

| part | verdict |
|---|---|
| **iCE40 HX8K/LP8K** (no DSP) | CIC decimation and TDM routing only. No beamforming. Cheapest way to get 8–16 capsules onto one clock |
| **iCE40 UP5K** (8 DSP, 128 KB SPRAM) | + fractional-delay alignment, ≤16 beams, bounded-lag correlation for ~8–16 mics. **The sweet spot for a first array** |
| **Zynq-7000** (220 DSP, ARM cores) | 32 capsules, 64 beams, full GCC-PHAT, and Linux on the PS. Also ~10× the power and cost, and moves the project's centre of gravity |

⚠️And the constraint that outranks all three: `docs/node-hardware.md` records that the good
microphone **has not arrived** and the nodes still run the onboard PDM part, and
`firmware/boards/README.md` records that a derived-not-measured pin map has *already* reached a
soldering iron twice in this project (the L86 1PPS; the PUC PPS pad). An array is 8–32× that
risk surface. **Nothing in section 4 should be bought before a two-capsule coherent-sampling
bench test exists**, and that test is cheap: two capsules, one clock, a known-geometry
source, and a measured delay compared against d/c.

---

## 5. What this survey concludes

### 5.1 Ranked, with the reason each rank is where it is

| # | candidate | cost | risk | verdict |
|---|---|---|---|---|
| 1 | **TKEO as a confirmation statistic on the gate** | 4 op/sample, **62.5 µs latency**, ~0.03 % of a core | low — additive, changes no axis | **Strongest.** The only candidate that is inside the 183 µs timing budget |
| 2 | **Sustained-source detection on the existing scene lane** | tens of op/s; the features are already computed and stored | low — offline-testable against 228k stored rows before any firmware change | **Largest unexploited asset.** Chainsaw/motorcycle are undetectable today (§3.5) |
| 3 | **Band-domain spectral flux over scene slices** | ~3.75 k op/s | low | Separates transient from level shift; 16 ms latency, so classify-only |
| 4 | **Event-gated DS-CNN-S via ESP-DL INT8** | ~2.7 M ops/inference at 1–10 /s = 1–11 % of a core, ~38 KB ×2 OTA slots | medium — PSRAM placement, OTA size, corpus needed | Affordable and worth prototyping. **Label only, never timestamp** |
| 5 | **Energy STA/LTA alongside the envelope gate** | ~2 op/sample | medium — a second detector changes the corpus | Free to compute, not free to adopt |
| 6 | **iCE40 UP5K front end for an 8–16 capsule array** | new hardware, new toolchain, new golden-vector harness | **high** | Only after a two-capsule coherent-sampling bench test |
| 7 | **Continuous neural inference (100 /s)** | 113 % of a core unaccelerated | high | **Refuse.** Does not fit, and §2.3 says the failure mode is silent audio loss |
| 8 | **Edge Impulse / hosted feature extraction** | — | high | **Refuse.** Non-reproducible band axis vs a frozen wire profile and a 228k-row corpus |
| 9 | **ESP-SR VADNet/WakeNet for detection** | 22.6 % of a core VENDOR-CLAIM | high | **Refuse.** 1–3 frame onset delay is 50–150× the timing budget; speech is not the target class |
| 10 | **Porting `gunshot_logmel_logistic_v1` to ESP-DL** | — | — | **Refuse.** A single `Gemm` gets 2.36×, on 2560 MACs that are already free |

### 5.2 The three findings that would survive deletion of everything else

1. **Split timing from labelling.** The energy gate keeps the UTC instant and its 4.667 ms
   group-delay correction. Any model produces a label and a confidence and is forbidden from
   producing a time. Every neural candidate's onset ambiguity (10–30 ms) exceeds the system's
   entire timing budget (183 µs) by two orders of magnitude.
2. **The DMA depth is the compute budget.** ~10 ms of p99 per 16 ms block for an inline stage
   (§2.3). A stage that exceeds it does not run slow, it drops audio, and nothing reports it.
3. **Two of the three named target classes cannot fire the gate at all** (§3.5), and the lane
   that could detect them — 20 bands at 62.5 fps, already computed, already stored — is
   classified by nothing. That is the cheapest available improvement in this document and it
   needs no new silicon, no new model format and no firmware change to *evaluate*.

### 5.3 What must be measured before any of this is adopted

None of these is a build. All are measurements, and all but the last can be done offline
against data already on the cards:

1. **Free internal SRAM on a running node**, under Wi-Fi + HTTP + SD load. §1.3 is honest that
   this is UNVERIFIED, and it decides whether any model fits at all.
2. **p99, not mean, of one `audio_pump()` block** under the same load — the input to §2.3's
   rule. The mean is already known to fit; the mean is not the question.
3. **TKEO and spectral flux replayed over stored clips and scene rows**, offline, scored as
   false alarms per node-hour at fixed recall (§3.5) — not as accuracy.
4. **A sustained-source test over the stored scene corpus**, same scoring. The 228k rows exist.
5. **PSRAM-resident INT8 inference latency on an S3**, if and only if 1 and 2 leave room.
6. **A two-capsule coherent-sampling bench test**, before any FPGA part is bought (§4.6).

### 5.4 Observed while surveying, for an owner to confirm

`firmware/gen_decim.py`'s prose says *"a 375 Hz transition, which is what makes this 257 taps
rather than a cheap halfband"*, while the module constant is `TAPS = 449` and the MEASURED
block in the same docstring reports *"449 taps × 256 outputs"*. The 257 appears to be a stale
figure from an earlier design; the filter that ships is 449 taps. **Not changed here — this is
a docs-only survey and the file is firmware.** Flagged so the number that is quoted downstream
is the one the silicon runs.

---

## 6. Sources

In-tree, MEASURED: `firmware/hear_node/hear_node.ino`, `firmware/hear_node/mel_impulse.h`,
`firmware/hear_node/mel_scene.h`, `firmware/gen_decim.py`, `firmware/gen_mel.py`,
`firmware/gen_mel_scene.py`, `firmware/boards/xiao_s3_sense.h`, `firmware/boards/README.md`,
`modules/gunshot/train_gunshot.py`, `model_gunshot_48k.json`, `docs/node-hardware.md`,
`docs/acoustic-stack.md`, `docs/ml-lifecycle.md`, `docs/REDESIGN-LESSONS.md`.

External, VENDOR / VENDOR-CLAIM, retrieved 2026-09-15: ESP-DL README and
`benchmark_report.md` v3.3.10 (generated 2026-09-01); ESP-SR wake word engine, VADNet and
benchmark documentation; Edge Impulse Studio processing-block documentation.

External, DATASHEET: ESP32-S3 technical reference; Lattice iCE40 UltraPlus and iCE40
LP/HX family data; AMD/Xilinx Zynq-7000 family data; Arm Cortex-M4 and Cortex-M7 technical
reference manuals; MSM261D3526H1CPM V1.2 (via `hear_node.ino:337-342`).

External, UNVERIFIED: Speech Commands / Hello-Edge / MLPerf-Tiny KWS model size and operation
counts. Used only to order candidates, never as an acceptance criterion.
