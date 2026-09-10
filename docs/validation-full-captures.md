# Validation on untouched captures

Every earlier test used audio chosen because it contained shots, or concatenated event tracks
with the dead air stripped out. Neither can measure a false-alarm rate. This run is the raw
captures: **27 chunks, 123.7 minutes**, across all four 2026-09-05 sessions, mostly nothing.

## Result

| | before re-arm fix | after |
|---|---|---|
| raw detections | 338 | **168** |
| impossible clusters (>2 rounds inside 0.5 s) | **32** | **1** |
| false alarms in 68.5 min of quiet audio | **0** | **0** |

Rounds per string afterwards: 21 singles, 20 doubles, 6 triples, tailing off to one 19-round
string. Longest reads 19 rounds over 9.4 s at 1.9/s, then 14 over 13.9 s — shooting, not
artefacts.

**46% of detections land inside known firing windows covering 3.3% of the audio** — 14x
enrichment over chance.

⚠️**THIS TABLE IS SUPERSEDED AND HAS NOT BEEN RE-MEASURED (2026-09-09).** It was produced by a
gate whose ambient floor ran at a single symmetric `AMBIENT_TAU_S = 0.2083 s`, updated only while
the envelope was below threshold. That is no longer the default. The floor is now asymmetric in
direction -- `AMBIENT_TAU_RISE_S = 30 s`, `AMBIENT_TAU_FALL_S = 5 s`, tracked unconditionally --
because the old form let a burst raise its own threshold.

The evidence, measured on the node `mach` while clapping in the same room: ambient 22 -> 143 in
five seconds, threshold 200 -> 1146, and **not one clap detected**, while a phone beside it
recorded every one. Across the three nodes the one in the occupied room had the highest peak
envelope (16938, 2.7x its siblings) and the **fewest** detections (294, against 689 and 470).
The premise this table's constant rested on -- "the floor only ever sees material already below
threshold, so nothing it tracks is an event" -- is false: a transient's reverberant tail is below
threshold and *is* the event.

**What that means for the numbers above.** As this document already warned, they are a property
of that constant and not of the algorithm, so they do not describe the shipping gate. Expect the
raw detection count to RISE under the new default, most in occupied or bursty conditions; the
false-alarm figure (0 in 68.5 min of quiet) is the one most at risk, because a slower-rising
floor sits lower. **Re-measurement against the 2026-09-05 captures is outstanding.**

The old behaviour stays reproducible -- `Gate(fs, ambient_tau_s=AMBIENT_TAU_S)` sets both limbs
equal -- and `tests/test_node.py::TestAmbientTau::test_the_legacy_symmetric_constant_is_still_reachable`
pins that, so this table can be regenerated rather than merely believed.

⚠️**The 0.9631 sketch AUC quoted below predates the onset fix** and needs refitting; see
`docs/uplink.md`.

## The bug this run exposed

A fixed guard interval **cannot distinguish a decay tail from a new round.** It only suppresses
detections closer together than the guard, so a shot with a 100 ms tail re-fired every 25 ms for
as long as the envelope stayed high — producing clusters of 4–5 "rounds" spaced at exactly the
guard. 32 such clusters existed in 123.7 minutes of audio.

Fixed with a Schmitt re-arm: after firing, the gate will not fire again until the envelope has
fallen back below `REARM_FRAC` of threshold. Regression tests cover a 300 ms tail collapsing to
one detection, two genuine rounds 200 ms apart staying two, a 700 rpm burst NOT being collapsed,
and the decay crossing a DMA block boundary.

⚠️Both numbers matter and they pull in opposite directions. A gate tuned only to kill duplicates
will swallow full auto at 85 ms spacing; one tuned only to catch full auto will re-fire on every
tail. The tests pin both ends.

## What this does not establish

- **Nothing about node CPU cost.** The wall-clock figure here is Python on a Pi 5 and says
  nothing about an nRF52840. The algorithmic cost is ~3 ops/sample — about 0.23% duty at 64 MHz —
  but that is arithmetic, not a measurement on the part.
- **No labels.** These 168 detections are unlabelled; the sketch AUC (0.9634 nested, a tie with
  the hand features, and ⚠️peak-aligned so superseded by the onset change) comes from a
  different, hand-labelled set. Enrichment into known windows is corroboration, not ground truth.
- **One board, one session, one site.** Rear board only, 2026-09-05, one range.
