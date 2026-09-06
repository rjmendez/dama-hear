# Module: bioacoustic (planned)

Cicadas and katydids — the original reason for putting microphone arrays on the robot.

Nothing here yet. It is a separate module rather than a mode of the supersonic one because the
signal and the geometry genuinely differ:

| | supersonic | cicada / katydid |
|---|---|---|
| signal | impulsive, broadband, ~100–300 µs | sustained, narrowband, tonal, periodic |
| source | moving, radiates off a Mach cone | stationary point |
| detector | level gate + impulse shape | band energy + periodicity |
| solver | `hear/solve/shockwave.py` | ordinary TDoA hyperbolae (to write) |
| output | line of fire, miss distance | position, and with it a census |

What carries over unchanged: PPS-disciplined per-node time, the survey and local ENU frame,
the labelling loop with active-learning triage, and the classifier training pipeline.

What a sustained tonal source makes *easier*: it is periodic, so you can integrate over many
cycles instead of living or dying on a single 100 µs transient — which means sample rate stops
being the binding constraint it is for the supersonic module.
