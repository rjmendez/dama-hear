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

What exists is the platform in Python: detection gate, log-mel sketch, telemetry packing,
shockwave solver, and a trained classifier. All of it runs off recorded audio with no hardware
attached, because the node firmware has to be provable before a wire is cut. **The node firmware
does not exist yet.**

    pip install numpy pytest && python -m pytest tests -q

## Running it

Everything is driven from recorded audio. `hear/node/pipeline.py` takes samples in and produces
the exact bytes a node would transmit:

```python
from hear.node.pipeline import Pipeline, summarise
dets = Pipeline(fs).run(samples)      # samples: one channel, int16-scaled float
summarise(dets)                       # counts it can defend, not counts it invents
```

The field captures behind `docs/findings-2026-09-05.md` are not redistributable. To run the
recorded-audio test against your own, set `DAMA_HEAR_REAL_WAV` to a single-channel WAV.

## Building a node

`docs/node-hardware.md` is the bill of materials. `docs/faketec-pin-budget.md` works out what a
fakeTec/ProMicro carrier actually leaves free — the sensor set needs exactly the four GPIO the
board has spare — and why the nRF52840 cannot sample at 48 kHz. Read both before ordering parts.

## Licence

GPL-3.0. See `LICENSE`.
