# Phone capture-path calibration: field runbook

`tools/hear_latency_cal.py` measures ONE number per phone: how far its acoustic onset timestamp
sits from the truth, in milliseconds. Until that number exists and is inside the array's bias
bound, `hear/nodeclass.py` refuses the phone as a TDoA arrival source outright -- see the
`gotchi-phone` class entry's module docstring for why a capture-path bias cannot be recovered
later from a residual, no matter how good the phone's clock is. This is the field half of closing
that gate. It is not the clock half: a phone's 5 ms `GPSTimingSync` clock tier is a separate,
still-open problem this tool does not touch, and `--admit-heterogeneous-receivers`
(`tools/hear_tdoa.py`) stays a deliberate operator choice even once both are closed.

Read `python3 tools/hear_latency_cal.py --help` before a field session; this runbook explains what
its output means, not a substitute for it.

## What it actually measures

The app corrects `corrected = raw + offset`. This tool solves for `offset` by putting a phone next
to a `xiao-s3-pps` node (25-30 ns disciplined by GPS PPS -- four orders below the millisecond being
measured, so for this purpose the node's timestamp IS truth) and subtracting: for every acoustic
event both devices heard, `offset = node_onset_utc - phone_onset_utc`.

## 1. Where to stand

**Co-located. Not nearby.** The subtraction above is only correct if the phone and the node hear
the same wavefront at the same instant -- true only if they occupy the same point. The tool cannot
detect a violation of this; it has no second observation to check it against.

Every metre of separation costs `1000 / 343 = 2.915 ms` of bias that lands directly in the offset,
silently, with nothing in the output flagging it. On myasshurts' own measured 13.122 ms this is not
a rounding error: 1 m of "close enough" is 22% of the number you are trying to measure. Put the
phone touching the node's enclosure (same table, same mic height if the node's mic is exposed),
not across the room and not "close, basically."

Pick whichever `xiao-s3-pps` node (nyquist, mach, rankine) is easiest to co-locate with for the
session -- the tool takes the node NAME as `--pair PHONE=NODE`, and any of the three is an equally
valid reference; nothing about the offset it reports depends on which one you pick.

## 2. Before you clap

The tool reads the **pool**, not the phone or the node directly -- `--pool` must already hold both
the phone's MQTT-drained rows and the node's dets.csv-drained rows for the session window. Drain
first:

    python3 tools/hear_drain.py --pool ~/hear-pool \
        --node <node>=<node-ip> --phone-corpus <mqtt capture dir>

then confirm the phone and the node both appear anchored in that pool for your session's time
range before running the calibration tool -- an empty pool and a genuinely silent room look
identical to `hear_latency_cal.py` (see the "no coincidences" refusal below), and only the drain
step can tell them apart.

Pass `--since <unix seconds>` set to the moment you actually placed the phone, so an earlier
session's rows (a different position, a different day) cannot leak into this fit.

## 3. How many claps

The fit needs `--min-pairs` (default **12**) coincident events that agree with each other
(`--max-mad-ms`, default **8.0 ms** median absolute deviation) before it will report anything.
Plan for more than the minimum:

- **20-30 claps**, a few seconds apart. A few seconds keeps events from overlapping inside the
  `--window-ms` (default 60 ms) matching window; a few dozen gives the fit room to lose some to
  mismatches (a reflection, a double-clap) and still clear 12.
- **One clean impulse each**, not a clap-clap flourish -- the tool pairs nearest-event, one node
  event per phone event, so two impulses closer together than the window can steal each other's
  match.
- Loud enough that BOTH devices' onset gate fires. If the node's dets.csv shows detections for
  your session but the phone's sketch stream does not (or vice versa), the events are not loud
  enough for the quieter of the two -- check both before assuming co-location failed.

Run it read-only first, without `--emit`, and look at `n` and `MAD` before deciding whether to
clap more:

    python3 tools/hear_latency_cal.py --pool ~/hear-pool \
        --pair myasshurts-9669aa0e=mach --since <session start>

## 4. What it refuses, and why

Every refusal below is a REPORT, not a crash -- the tool always tells you which of these fired and
why, per pairing, and moves on to the next `--pair` rather than stopping the whole run.

| Refusal | Cause | What to do |
|---|---|---|
| `SKIPPED ... has no anchored records` | the phone or the node has zero anchored rows in the pool for this window | check the drain step (`Section 2`); this is an ingest problem, not a calibration one |
| `SKIPPED ... no coincidences ... inside +-N ms` | the two streams share no event inside `--window-ms` | **two causes that look identical**: (a) nothing was heard together (wrong room, wrong time window), or (b) the true offset is LARGER than the window, so it exists but is off the end of the ruler. Widen `--window-ms` before concluding (a) |
| `REFUSED: only N coincident events (need M)` | `f["n"] < --min-pairs` | clap more; each `--pair` run is independent so re-running costs nothing already measured |
| `REFUSED: MAD X ms exceeds Y` | `--max-mad-ms` (default 8.0 ms) exceeded -- the matched pairs are not one population | suspect separation (Section 1), reflections, or a second sound source in the room during the session; re-run cleaner, do not average through it |
| `REFUSED: median X ms exceeds the app's own N ms plausibility bound` | `MAX_PLAUSIBLE_OFFSET_MS = 100.0` -- `AcousticRangingCollector`'s own ceiling | the phone would refuse this number even if written (`OFFSET_OUT_OF_RANGE`); a value past this either means genuine hardware pathology on that handset or a measurement error upstream (wrong `--pair`, clock not actually PPS-locked on the reference node) -- do not `--emit` it, investigate first |

A `REFUSED` pairing is never written by `--emit`, whether or not `--emit` is passed -- only a fit
that clears every gate above is merged in.

**What "usable" is silent about, and must not be assumed from this table alone:**
`hear/nodeclass.py`'s bias bound is **91.5 us = 3.1 cm** of RANGE -- a "usable" fit from this tool
routinely reports a MEDIAN in the tens of milliseconds (metres of range), which is still refused
by the arrival gate even once it is written and applied. Writing a value here retires the "never
measured" refusal (`path_bias_s is None`); it does not by itself retire the "too large" refusal.
Check the written number against `ARRIVAL_PATH_BIAS_MAX_S` (`python3 -c "from hear import
nodeclass as NC; print(NC.ARRIVAL_PATH_BIAS_MAX_S)"`) before expecting the phone to be admitted.

## 5. Getting the result into `acoustic_latency_calibration.json`

    python3 tools/hear_latency_cal.py --pool ~/hear-pool \
        --pair myasshurts-9669aa0e=mach --pair fancyantsy-96b6d5a8=nyquist \
        --since <session start> --separation-m 0.0 \
        --emit acoustic_latency_calibration.json

- `--emit` **merges**, it never rewrites: an entry a previous session measured and this run did
  not re-measure survives. Safe to run once per phone per session and accumulate over separate
  field days.
- `--separation-m` records the operator's OWN accepted separation (0.0 if genuinely touching) and
  is used only to PRINT the bias it implies -- it is never subtracted from the fit. State it
  honestly; a nonzero value here is a documented admission that a real number is missing, not a
  correction.
- Only phones with a usable fit this run are written. `_measured` in the output file carries the
  provenance (`reference_node`, `n_events`, `mad_ms`, `stated_separation_m`, `window_ms`) so a
  later reader does not have to trust a bare number.
- **The file this writes is a scratch copy of the real one.** The app reads
  `android/app/src/main/assets/acoustic_latency_calibration.json` inside the `dama-gotchi` repo,
  bundled into the APK at build time -- it is NOT fetched at runtime. Getting a measured offset
  onto a phone therefore needs, in `dama-gotchi`, not this repo:
  1. merge the emitted file's `by_node_id` entries into
     `android/app/src/main/assets/acoustic_latency_calibration.json`,
  2. rebuild the app,
  3. redeploy to the handset (the operator's existing OTA/install path).

  This runbook stops at step 0 -- producing the number -- because steps 1-3 are a `dama-gotchi`
  build/deploy action, not a `dama-hear` one, and this repo does not touch that checkout.

## 6. Where the fleet actually stands, measured 2026-09-10/11

`android/app/src/main/assets/acoustic_latency_calibration.json`, read from the `dama-gotchi`
checkout:

    {
      "schema": "acoustic_latency_calibration.v1",
      "by_node_id": {
        "myasshurts-9669aa0e": 13122000,
        "financialdistress-a4a491b0": 293499000
      },
      "by_model": {}
    }

Three phones, two entries, and **neither is fully usable today**:

- **myasshurts** -- 13.122 ms (4.5 m of range). Below the app's 100 ms plausibility bound, so it
  IS applied on-device. Still 143x `nodeclass.py`'s 91.5 us bias bound -- applied, and still not
  small enough to admit the phone as an arrival source without a real re-measurement.
- **financialdistress** -- 293.499 ms. Past `MAX_PLAUSIBLE_OFFSET_MS`, refused on-device, NOT
  applied. This phone runs with zero capture-path correction today despite having an entry --
  the entry's presence is not evidence of correction.
- **fancyantsy** -- absent. No entry at all, `by_model` is empty so the model fallback cannot
  cover it either. Zero correction, same as financialdistress in effect, with no number on record
  to even be wrong.

So "two of three phones have no usable entry" is financialdistress (present, refused) and
fancyantsy (absent) -- not the same failure, but the same practical result: uncorrected.
