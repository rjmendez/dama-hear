# Architecture

## One idea

Nodes never agree on time. Each disciplines its own clock to its own GPS PPS (~30 ns) and
timestamps what *it* heard. The network carries results, not audio, and not time.

That decoupling is what makes a slow lossy radio viable: LoRa latency, jitter, retries and mesh
hops are all irrelevant to a measurement that was timestamped before it ever reached the radio.

## Layers

```
modules/<name>/     what you are listening for: detector, features, classifier
hear/solve/         geometry: shockwave (moving source), point (stationary) [planned]
hear/               platform: timebase, events, features, survey, labelling
```

A module owns the sound. The platform owns the clock, the survey, the solving and the labelling
loop. Adding cicadas should not touch anything the supersonic module needs.

## Uplink

Detect at the edge, ship the result:

```
8 B  PPS-disciplined UTC timestamp
14 B 7 features (float16)
4 B  classifier score + node id
---- ~26 B per event; a 10-round string packs to ~70 B with delta-times
```

70 B is 0.10 s of LoRa airtime at SF7 and 0.32 s at SF9. The same string as raw audio is ~0.5 MB,
which fits no spreading factor at all.

⚠️**SF10 and above exceed the FCC Part 15.247 400 ms dwell limit** for a 70 B payload
(571 ms / 1273 ms / 2240 ms). The long-range settings are the ones you cannot legally use.

Full audio stays on the node for training and verification, retrieved on a service visit or over
opportunistic WiFi. Do not try to move audio over the mesh.

## Labelling

The detector is a level gate; the operator's ear is the ground truth. The loop is: detect →
score → queue **only the uncertain band** (p 0.35–0.65, measured at 14% of events) → label →
refit. Labelling everything does not scale; labelling a seventh of it does.
