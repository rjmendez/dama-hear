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
- **No labels.** These 154 detections are unlabelled; the 0.9631 sketch AUC comes from a
  different, hand-labelled set. Enrichment into known windows is corroboration, not ground truth.
- **One board, one session, one site.** Rear board only, 2026-09-05, one range.
