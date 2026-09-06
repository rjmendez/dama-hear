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
