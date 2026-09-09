# Module: supersonic projectile

Detects and locates incoming supersonic rifle rounds.

**Why this is the hard case, and therefore first.** A suppressor works on the muzzle blast. It
cannot touch the shockwave the bullet drags, because the bullet is supersonic whatever is on the
end of the barrel. So against a suppressed shooter past blast range the crack is the *only*
signal — and it is the one that tells you a round came near you rather than that someone fired
somewhere.

## What it gives you, and what it does not

The crack radiates off the Mach cone, not from the muzzle. So it yields the **trajectory**:
a line of fire and a miss distance. The shooter is somewhere back along that line, **range
unknown**. Closing that needs one of:

1. the muzzle blast and the crack–blast interval — which suppression denies you;
2. N-wave duration → miss distance per node, needing far higher sampling AND a different
   microphone -- the ICS-43434 low-passes above 24 kHz, so shape is out of reach on that
   part at any rate. Changing the MCU alone does not buy this;
3. bullet deceleration between nodes, fitted to the round's drag curve — no blast required,
   and the only one that works against a can.

## Contents

- `classify.py` / `model.json` — shot vs not-shot. Six weights and a bias; no sklearn, no GPU.
  Grouped-CV AUC 0.959 on 228 operator-labelled events.
- `train.py` — refits the model. Run it before trusting the weights anywhere new.

Trajectory solving lives in `hear/solve/shockwave.py`, because the geometry is platform-level.

## Before you trust this anywhere else

Trained on one range, one rifle, one afternoon, all cracks and **zero blast-only** examples.
A shot heard from behind, or a subsonic round, is not represented. Retrain.

## Scoring the bytes the fleet transmits

`model.json` reads six hand-engineered features. Nothing consumed a **sketch**, so the fleet was
transmitting a representation no model could score. `train_sketch.py` fits ones that can, and
`classify.score_sketch()` applies them:

| model | bands | applies to | nested grouped-CV AUC |
|---|---|---|---|
| `model_sketch.json` | 20 | ≥32 kHz sensors (the phones) | 0.9728 |
| `model_sketch_15.json` | 15 | ≥16 kHz — **the whole fleet** | 0.9665 |

⚠️**Both figures were measured at 48 kHz, and the fleet runs at 16.** Every pooled node frame is
16 kHz, and this repo has separately measured what a 48 kHz-fitted fixed-bank model does on 16 kHz
audio over the common 15 bands: **0.9473** (`hear/corpus.py:283-285`, `hear/sketch.py:124-130`).
That, not the column above, is the number that applies to a node score. ⚠️**And the column above is
optimistic**: nested grouped CV grouped events by `int(utc // 3)` = 3 s, while
`hear.validate.decorrelation_lag_s` measures these features not decorrelating until 22.4 s apart —
91.7 % of events have a different-group neighbour inside the lag (`validate_sketch.py`).
`tools/hear_score.py` ships all three numbers on every row for this reason.

⚠️The earlier values in this table (0.9634 / 0.9588) were the **pre-onset-fix, peak-aligned**
numbers from `train_sketch.py`'s notes; the shipped files' own `auc_nested_grouped_cv` keys are the
ones above. Corrected 2026-09-09 when `hear-score` became the first consumer to read them.

⚠️**The sketch is not more accurate than the six features.** Paired bootstrap over the 69 groups:
+0.0054 AUC, 95 % CI [−0.0098, +0.0232], P(better) 0.75. A tie. Earlier notes on this repo implied
otherwise and have been corrected. The reasons to use it are that it fits a Meshtastic packet, that
it can be retrained for a sound nobody has thought of yet, and that a node cannot reliably compute
the envelope those six scalars come from.

⚠️**`score_sketch` refuses rather than pads.** A 16 kHz node's top five bands are empty *by
construction*; feeding those to the 20-band model is a spectrum claiming the node measured silence
above 9.4 kHz when it measured nothing at all. Measured cost: AUC 0.9141 against 0.9473 on the same
audio. Use `FLEET_SKETCH_MODEL` for a mixed-rate fleet.

⚠️**Do not re-try burst structure.** Round-to-round interval, capped at 250 ms so it cannot encode
an operator's pause: 0.9634 → 0.9630. *Alone* it scores AUC **0.32** — anti-predictive, because
short intervals mark retriggers and retriggers are labelled not-shot.
