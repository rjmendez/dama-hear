# Detector lanes are profile-specific. The event schema is not.

`design-event-detector` designed one detector (`TonalGate`, and the `BioPipeline` caller that
tags its output). It did not say which nodes can afford to *run* it, or how it must be run
differently on a node that sleeps between events versus one that never turns off. This is that
design. It changes no runtime behaviour by itself: `hear/nodeclass.py` now declares the fact
(`power_profile`, `has_camera`, `detector_lanes()`) that everything below is checked against.

## Why one universal detector is the wrong shape

`hear/nodeclass.py` already refuses to average an NTP node into a PPS solve because the two are
different MEASUREMENTS wearing the same units (see its module docstring). Power budget is the
same kind of fact, on a different axis: a solar Meshtastic node and a mains ESP32-S3 do not differ
in what sound they can *hear* -- they can carry the identical ICS-43434/PDM part -- they differ in
how much continuous compute they can spend deciding whether something worth reporting happened.
Running `TonalGate` (a Hanning-windowed FFT every `hop_s` = 32 ms, forever) on a solar node's power
budget is not a slower version of the mains node's detector; it is a different device that goes to
sleep or goes dark, which is exactly the "disarmed and unreachable" failure
`hear/node/detect.py`'s own docstring already treats as unacceptable for the impulse gate. The fix
is not a bigger battery, it is not running the same lane on both.

## The two profiles that exist today, and the one that does not yet

| profile | `power_profile` | example class | radio | budget that decides everything |
|---|---|---|---|---|
| solar Meshtastic tier-1 | `solar_duty_cycled` | `xiao-s3-pps`, `xiao-s3-i2s` | LoRa, 237 B/packet | joules per day, off a panel + battery |
| mains sense/webcam | `mains_continuous` | `esp32s3-cam-mains` (design target, unbuilt) | WiFi | none -- CPU/RAM/flash bind instead |
| mains listener (built) | `mains_continuous` | `puc-pps`, `puc-ntp` | WiFi | none (USB-powered BirdWeather PUC) |
| carried | `battery_mobile` | `gotchi-phone` | phone data | charged intermittently, shared with the rest of the phone |

`NodeClass.detector_lanes()` is the single source of truth for what follows; the table above is
the human-readable form of it, and `tests/test_nodeclass.py`'s
`TestPowerProfile`-equivalent tests pin the two from drifting apart.

## The common contract every profile must honour

Every detector on every profile emits into ONE event shape, regardless of which lane produced it.
This is not a new format: it is the union of fields `hear/node/detect.py`'s `Gate`,
`hear/node/bio_pipeline.py`'s `BioPipeline`, and (design-only, below) a camera trigger already
independently settled on, made explicit so a fourth detector does not have to reinvent it:

```
index / t_s          onset, in the units that detector can actually measure (sub-sample for
                      Gate, frame-resolution for TonalGate -- see tdoa_capable below)
detector              "impulse" | "tonal" | "camera"  -- which lane produced this event
burst_kind            free-text sub-type ("cicada", "katydid", "vehicle", "aircraft", ...);
                      BioPipeline's per-band tag generalises directly to any lane
confidence             float in [0, 1] or None; never a plausible-looking default (see below)
tdoa_capable          bool, STAMPED by the producer, never inferred by a consumer. False for
                      every current tonal/camera event (see bio_pipeline.py's module docstring
                      for why); True only for Gate's sub-sample onset, and only on a gps_pps
                      class. A caller that skips this and hands a frame-resolution onset to the
                      TDoA solve is repeating the exact mistake nodeclass.py's require_arrival()
                      exists to make impossible for a whole node class -- here it is per event.
node_class            the hear/nodeclass.py name this event's producer ran on, so a consumer can
                      call detector_lanes() and CHECK the event came from a lane that class can
                      actually afford, rather than trusting the label.
```

⚠️**`confidence: None` is not `confidence: 0.0`, and a schema that coerces the two together lies
by omission.** `TonalGate` already draws this line (structure < 0 returns `None` from `_close`,
never a fabricated low score); the contract generalises it. A profile that cannot run a lane
(camera trigger on a node with no camera) must not emit a null-confidence stub for it either --
absence of a field is the honest report, not a zero-filled one.

⚠️**RAW AUDIO IS NOT PART OF THE UPLINKED EVENT ON ANY PROFILE, AND THAT IS A RADIO-BUDGET FACT,
NOT A CAPABILITY ONE.** `hear/node/pipeline.py`'s 172-byte sketch already draws this line for the
impulse lane; every lane below inherits it. `raw_retain_s` (already a `NodeClass` field) says how
much raw audio survives ON THE NODE for later pull, independent of what got shipped -- and that
budget scales with the profile: a solar node's SD card is the same part as a mains node's, but the
solar node cannot afford to key WiFi/SD writes as often, so its retained window trades against the
same joule budget as everything else in this document.

## Lane 1: `impulse_gate` -- unchanged, and already the right shape for every profile

`hear/node/detect.py`'s `Gate` is a 1 ms boxcar and a threshold comparison. It is cheap enough that
every profile in the table above runs it, including the duty-cycled solar node -- this is not new
work, it is the existing baseline that the rest of this document adds LANES beside, not replaces.

## Lane 2: `tonal_gate_duty` -- the solar/Meshtastic answer to "sustained sound, on a battery"

**The problem `TonalGate` was NOT designed against.** `modules/bioacoustic/detect.py`'s own
docstring times its cost: `nfft=1024` (64 ms), `hop_s=0.032`, a full FFT and a Hanning window
every hop, continuously, is the assumption baked into `floor_tau_up_s=300`, `close_s=0.25` and
every other constant in that file. That is a fine trade for a mains node. It is not a fine trade
for a device whose entire purpose is to spend most of its life asleep.

**What a solar node CAN afford, cheaply, continuously.** The `impulse_gate`'s own envelope (a
boxcar over `|x|`) is already running whenever the node is awake at all, because it is the trigger
that decides whether to wake the radio. That buys band-limited energy almost for free if the
in-band filtering happens BEFORE the boxcar rather than after a full-band FFT: a low-order IIR
band-pass (or, cheaper still on an MCU with no FPU-heavy budget to spare, a Goertzel-style
single-bin power estimate per band of interest) followed by the same 1 ms-boxcar-and-threshold
machinery `Gate` already has, run in the ALREADY-AWAKE window rather than as a reason to stay
awake. This is `tonal_gate_duty`: TonalGate's THREE axes (band, structure, time) are not simplified
away, they are evaluated over a SHORTER, DUTY-CYCLED capture rather than a continuous one --
concretely:

  1. The node's existing wake trigger (impulse over threshold, or a periodic health/telemetry
     wake it already does) opens a short capture window -- long enough to measure `min_duration_s`
     for the target call length, not indefinitely.
  2. Inside that window ONLY, run the real `TonalGate` machinery (full FFT, real flatness,
     real periodicity) at its existing constants. The FFT cost is now bounded by the window
     length, not by the node's uptime.
  3. Outside the window, run the cheap band-limited comparator above as the thing that decides
     WHETHER to open a window at all, the same role the impulse gate's threshold already plays.

**What this is not.** It is not "TonalGate with looser thresholds" -- the thresholds
(`TONALITY_MIN`, `PERIODICITY_MIN`) are properties of the STATISTIC, not of how often it is
computed, and loosening them to save power would reintroduce exactly the false-positive risk the
detection-literature memory session and `modules/bioacoustic/detect.py`'s own worked examples
were measured against. It is a duty-cycling of WHEN the expensive statistic runs, not a cheapening
of what it measures.

**What is genuinely lost, and must be stated rather than hidden.** A sustained call that starts
and ends entirely between two wake windows is missed by construction -- the same honesty
`hear/node/detect.py`'s own docstring already demands of the impulse gate's retrigger reporting.
Every `tonal_gate_duty` event should therefore carry `duty_cycle_s` (the wake/sleep period in
force when it fired) alongside the existing `TonalGate` fields, so a consumer can bound the miss
rate rather than assume continuous coverage that was never running.

## Lane 3: `tonal_gate_continuous` -- what a mains node buys with an unmetered power budget

On `mains_continuous` and (while charged/awake) `battery_mobile` classes, `BioPipeline` runs
exactly as designed: one `TonalGate` per band, continuously, no duty cycle, because the FFT cost
that constrains lane 2 is not a constraint here at all. Additionally, because RAM and SD are the
only scarce resources rather than joules, a mains node can run MORE bands simultaneously than a
solar node ever should (cicada, katydid, plus e.g. a vehicle/traffic-rumble band a solar node would
have to time-share against) and can retain raw audio continuously rather than in a small ring,
since `raw_retain_s` is an SD-capacity budget on this profile, not a joule budget.

This is `hear/node/bio_pipeline.py::BioPipeline` exactly as already implemented in the previous
session's work -- this document does not change that class. What changes is which `NodeClass`es
are permitted to invoke it unbounded: `detector_lanes()` is the check a caller makes before
choosing between `tonal_gate_duty` and `tonal_gate_continuous`, and a class without
`tonal_gate_continuous` in its lane set must use the duty-cycled form instead of the continuous one
even if the code would technically run.

## Lane 4 (design only, unbuilt): `camera_fusion` -- the wildlife-camera profile

`esp32s3-cam-mains` (registered in `hear/nodeclass.py`, `has_camera=True`, explicitly marked
UNBUILT/design-target the same way `puc-pps` is marked UNVERIFIED) is the node this lane targets:
a mains-powered XIAO ESP32-S3 Sense with its OV2640 camera populated, running `impulse_gate` +
`tonal_gate_continuous` exactly as any other mains node, PLUS a vision trigger.

**What correlates, and how.** An audio burst (any `burst_kind`, from either audio lane) and a
camera-motion/presence trigger that occur within a short shared window are ONE multimodal event,
not two independent rows a downstream consumer has to re-associate by hand. The mechanism this
repo already has for exactly that problem is `hear/solve/burstassoc.py`'s cross-sensor
association-by-shared-constraint discipline (there: one `tau` must explain a whole burst across
microphones; here: one short time window must explain an audio tag and a photo from the SAME
node, which is a strictly easier, single-node version of the same idea because there is no
propagation delay between a node's own ears and its own camera). A fused event adds two fields to
the common contract: `modality` (`"audio"` | `"image"` | `"audio+image"`) and `image_ref` (however
the pooled image is later addressed, e.g. a clip-store-style key mirroring `hear/clips.py`'s
existing pattern for audio clips).

**Feasibility split -- what actually runs where, and why.**

| task | ESP32-S3 (on-node) | Pi / server |
|---|---|---|
| envelope/level gate, band-limited power (Goertzel or small FFT) | **yes** -- already the shape of `impulse_gate` and `tonal_gate_duty` | n/a, too cheap to bother moving |
| full-rate `TonalGate` (1024-pt FFT @ ~31 Hz, flatness + envelope autocorrelation) | **yes, continuously, on a mains-powered board** -- this is exactly what lane 3 already assumes | also fine; not the bottleneck either way |
| Stage-0 gunshot logistic (`modules/supersonic/classify.py`, six hand features, one weighted sum) | **yes** -- it is a handful of multiplies over features the sketch already computes | yes, and is where it runs today |
| tiny quantised keyword-spotting-style CNN (~20-50k int8 params, TFLite Micro) | **yes, plausibly** -- ESP32-S3 has enough SRAM/PSRAM and Espressif ships exactly this class of model (`esp-tflite-micro`, `ESP-DL` wake-word examples); NOT YET ATTEMPTED in this repo | trivially yes |
| coarse motion/person/animal PRESENCE detection on camera frames (frame-diff, or a tiny quantised detector such as Espressif's `ESP-WHO` person-detection model) | **yes** -- this is the DESIGNED role of the camera trigger above, a coarse "worth keeping this frame" decision, not species ID | yes, and cheaper here too, but the point of doing it on-node is to avoid shipping every frame over WiFi for nothing |
| Perch 2.0 embeddings (EfficientNet-B3, ~12M params) or BirdNET-class species classification | **no** -- `docs/acoustic-stack.md` section 6.3 already sizes Perch's model at 12M params / ~91 MB fp32 for the head alone; nothing in the ESP32-S3's SRAM/PSRAM/flash budget holds that, and there is no float throughput to run it in anything resembling real time | **yes** -- this is Stage 1 in the existing design, and stays there regardless of which node captured the audio |
| video-frame species ID / full image classification | **no** -- same order-of-magnitude mismatch as Perch, worse: a vision CNN of comparable accuracy is larger, not smaller | **yes** -- image goes to the Pi/server exactly as `tools/hear_drain.py` already pulls clips off a node for pooling; nothing about the transport changes, only what gets pooled |
| cross-node TDoA solve | **no**, and never was -- this has always been a server-side job (`hear/solve/*`) | **yes**, unchanged |

**The one-line summary of the split, because it recurs above in every row:** the ESP32-S3's job in
this profile is deciding WHAT IS WORTH KEEPING (coarse, cheap, continuous, on-node), never WHAT IT
IS (expensive, off-node, exactly the same Stage-0/Stage-1 split `docs/acoustic-stack.md` already
uses for audio). A camera profile does not change that division; it adds a second sensor that
follows the identical rule.

**What is explicitly NOT decided here**, because it is a hardware/product choice this document
should not pre-empt: whether the camera node's compute stays on the XIAO's own ESP32-S3 or moves
to a co-located Pi for the vision path specifically (the audio path's answer does not depend on
this choice either way -- lanes 1-3 are unaffected by what runs the camera). `esp32s3-cam-mains`
is registered as a single-board design target because that is the node named in the requirement
this document answers; a Pi-attached variant is a different `NodeClass` if it is ever built, not a
retrofit of this one.

## What changed in code, concretely

- `hear/nodeclass.py`: `NodeClass` gained `power_profile` (`"solar_duty_cycled"` |
  `"mains_continuous"` | `"battery_mobile"`, validated at construction) and `has_camera` (bool),
  plus `detector_lanes()` deriving the lane set from both. Every existing registered class now
  states its profile explicitly rather than relying on a default. A new, explicitly UNBUILT
  `esp32s3-cam-mains` class is registered as the design target for lane 4, following the same
  "provisional, do not survey a real node as this class" discipline `puc-pps` already established.
- `tests/test_nodeclass.py`: validation and lane-derivation tests for the new fields.
- No change to `hear/node/detect.py`, `hear/node/pipeline.py`, `hear/node/bio_pipeline.py`, or
  `modules/bioacoustic/detect.py` -- lanes 1-3's *code* already exists from the prior session; this
  document is what says which `NodeClass` may invoke which, and lane 4 is design-only pending
  hardware.

## Open, and belongs to whoever builds lane 2 or lane 4 in firmware

- `tonal_gate_duty`'s wake/window/sleep policy is described here at the mechanism level; it has no
  firmware implementation yet, and no wake period has been measured against a real solar/battery
  budget. That measurement is the same kind of "not asserted, to be fitted the first night this
  detector records something a person has listened to" caveat `TonalGate`'s own thresholds already
  carry, and it should be fitted the same way: against a labelled capture, not a guess.
- `esp32s3-cam-mains` has no BOM, no pin map, and no firmware. `firmware/boards/README.md`'s
  profile-header discipline ("what is wired where and what the part can do... no behaviour, no
  policy") is the right home for it once a board exists.
