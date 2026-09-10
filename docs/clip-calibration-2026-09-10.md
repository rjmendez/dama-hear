# Clip calibration — 2026-09-10

The set gate condition 2 of `dama-hear/unsuspend-gate` asks for, and what it found.

**69 clips, one listener, two batches**, played through `tools/hear_listen.py` on gain-corrected
copies. Labels are in `testdata/clip-labels-2026-09-10.jsonl`. Nothing here is ground truth: one
person, one pass, no second opinion, and **only 2 of 69 were marked "sure"** — the other 67 are
"probably". Treat it as the first independent check that exists, not as a reference standard.

| node | clips |
|---|---|
| mach | 25 |
| nyquist | 23 |
| rankine | 21 |

| heard | n |
|---|---|
| insect | 28 |
| dog | 15 |
| machine | 12 |
| **nothing** | **9** |
| weather | 6 |
| vehicle | 3 |
| aircraft | 3 |
| bird | 1 |

⚠️**Batch 2 (40 clips) was drawn silence-stratified, batch 1 (29) at random.** Counts above are
therefore not a base rate for the site. Batch 1 alone gives insect 12 / dog 6 / machine 8 /
weather 2 / bird 1 / nothing 3 out of 29.

---

## 1. Gate condition 3 does not survive contact with the data

The gate said: *"`max_silence_frac` is set from the measured distribution in that calibration
set."* The distribution exists now, and it says the threshold should not be set at all.

### 1.1 The DSP `silence_frac` does not predict emptiness

`silence_frac` is the fraction of 20 ms frames below −60 dBFS. Against what the listener called
empty:

| | n | min | p25 | median | p75 | max |
|---|---|---|---|---|---|---|
| called **empty** | 9 | 0.080 | 0.365 | **0.460** | 0.670 | 0.905 |
| called **a sound** | 60 | 0.000 | 0.333 | **0.658** | 0.880 | 0.980 |

**AUC = 0.401.** Below chance, and in the wrong direction — the clips a person called empty are
*less* silent by this measure than the ones with something in them. A threshold on it discards
real sound at least as fast as it discards nothing:

| threshold | drops of the 9 empty | drops of the 60 with sound |
|---|---|---|
| 0.50 | 44 % | 63 % |
| 0.70 | 22 % | 45 % |
| 0.90 | 11 % | 17 % |

The mechanism is not mysterious: a quiet insect chorus is a low-level sustained texture that sits
under −60 dBFS most of the time, and an "empty" clip on a windy day does not. **Frame-level
level is not occupancy.** Nothing should be gated on this field.

### 1.2 The knob the gate actually names is a different thing, and it is nearly dead

`--max-silence-frac` in `check_tags` is not a per-clip filter. It gates `silence_top_frac`: the
fraction of tagged clips whose **model top-1 is `Silence`**, as a canary for *normalisation not
running*. Under YAMNet that canary worked, because an un-normalised clip came back `Silence`
every time.

Under `mn10_as` it does not:

| run | `silence_top_frac` | mean top score |
|---|---|---|
| normalised (healthy) | **0.0000** (0/69) | 0.254 |
| un-normalised (broken) | **0.0435** (3/69) | 0.162 |

A threshold between 0.000 and 0.0435 rests on **three clips**. The 95 % interval on a 3-in-69 rate
runs from about 0.9 % to 12 %, so a threshold at 2 % could miss a completely broken run. That is a
threshold from a single sample, which this repo forbids everywhere else.

### 1.3 What replaces it

The **run's mean top score** separates the same two conditions using all 69 clips instead of 3:

| clips in the run | healthy mean | broken mean | separation |
|---|---|---|---|
| 50 | 0.254 ± 0.032 | 0.162 ± 0.018 | 4.8 σ |
| 100 | 0.254 ± 0.023 | 0.162 ± 0.013 | 6.8 σ |
| **400** (the job's `--limit`) | 0.254 ± 0.011 | 0.162 ± 0.007 | **13.6 σ** |

The normalised score is higher on **58 of 69 clips (84 %)**; per-clip AUC 0.755, and the run-level
mean is what is gated, not the clip.

⚠️**This is calibrated against ONE failure — normalisation disabled.** A different break (wrong
weights, a corrupt resample, silence on the wire) may not move this number, and a floor that
passes is not proof the lane is healthy. It replaces a canary that could not fire at all; it does
not replace looking.

---

## 2. Model against human, for the first time

`mn10_as`, 69 clips, scored through the real pipeline. Human tags are mapped to AudioSet classes
by a **hand-written and deliberately generous** table in the comparison script — "in the
neighbourhood", not "equal" — so these percentages depend on choices a person made.

| | |
|---|---|
| model top-1 in the human's neighbourhood | **24/69 = 35 %** |
| model top-5 | 45/69 = 65 % |

The biggest agreement and the biggest disagreements:

| human said | model's top-1 | n |
|---|---|---|
| insect | **Insect** | 14 |
| insect | Speech | 7 |
| dog | **Animal** | 6 |
| weather | Insect | 6 |
| dog | Speech | 6 |
| machine | Insect | 5 |
| nothing | Hammer | 4 |
| nothing | Speech | 4 |

Three things fall out:

- **Insects are where the two agree.** Half the insect calls get `Insect` top-1, the single
  largest cell. That is also the one class this site plausibly has in quantity.
- **`Speech` is the model's null response.** 21 of 69 clips (30 %) return `Speech` top-1 —
  7 insect, 6 dog, 4 empty, 3 machine, 1 aircraft — on 3.5 wooded acres. It is what the
  model reaches for with nothing to say, and no downstream consumer should treat it as an
  observation.
- **The model has no "nothing here".** `Silence` was top-1 on **0 of 9** clips the listener called
  empty and 0 of 60 with sound. Emptiness has to come from somewhere else; the model's own top
  score is weakly informative (AUC 0.689) and the DSP measure is not (§1.1).

⚠️`weather → Insect` six times is worth its own look: wind in leaves and insect stridulation are
both broadband sustained textures, and this is the confusion most likely to matter for a site
whose ambient is trees.

---

## 3. What this set does and does not unlock

- **Gate conditions 1 and 2 are met.** 473 stored clips across three nodes over 2026-09-08…10,
  and 69 heard by a person including 25 from mach.
- **Gate condition 3 is retired rather than satisfied** — see §1 — and replaced with the
  score-floor canary.
- `tools/hear_tag.py`'s model card says a trainer *"needs a human-verified held-out set, which
  does not exist yet"*. One exists now. It is 69 rows from one listener with 2 marked "sure", so
  it is enough to **refute** things (it already refuted two) and not enough to **fit** anything.
  The card's refusal stands.
