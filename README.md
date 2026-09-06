# dama-hear

Distributed acoustic sensing across independently-clocked listening nodes.

Nodes hear the same event from different places. Each timestamps what it heard against its own
GPS PPS, so the network never has to agree on time — only on geometry. What a node ships is a
detection with a timestamp and a handful of features, not audio: a firing string packs to ~70
bytes, which fits a LoRa mesh, where the audio it came from would not.

## Why the split between platform and modules

The infrastructure is the same whatever you are listening to: synchronised capture, event
detection, node geometry, cross-node solving, labelling, training. What differs is the sound.

| | supersonic projectile | cicada / katydid |
|---|---|---|
| signal | impulsive, broadband, ~100–300 µs | sustained, narrowband, tonal, periodic |
| source | **moving**, radiates off a Mach cone | stationary point |
| geometry | trajectory line, cone half-angle `asin(1/M)` | point source, ordinary TDoA hyperbolae |
| what you recover | line of fire, miss distance | position, and with it a census |

Those want different detectors, different features and different solvers, but the same clock,
the same survey, the same labelling loop. Hence `hear/` (platform) and `modules/` (what you are
listening for).

## Modules

- **`modules/supersonic/`** — incoming supersonic rifle rounds. First module, because a
  suppressor silences the muzzle blast but cannot touch the bullet's shockwave, so the crack is
  the only signal and the hardest case. Ships a trained classifier and a trajectory solver.
- **`modules/bioacoustic/`** — cicadas and katydids. Planned. The original reason for putting
  microphone arrays on the robot.

## Status

Early. The supersonic module carries real field data and measured results (see
`docs/findings-2026-09-05.md`); the bioacoustic module is a placeholder. Nothing here is
deployed.

What exists is the platform in Python — gate, sketch, telemetry, solver, classifier — driven
from recorded audio. **The node firmware does not exist yet**, and the hardware it will run on is
not settled: `docs/node-hardware.md` is the sensor bill of materials, `docs/faketec-pin-budget.md`
is a closed route kept for why it closed.

    python -m pytest tests -q

The field captures are not redistributable. Point `DAMA_HEAR_REAL_WAV` at your own single-channel
WAV to run the recorded-audio test.

## Licence

GPL-3.0. See `LICENSE`.
