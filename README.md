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
from recorded audio. The node firmware is a **proof of concept only** — `firmware/` runs the gate
and sketch on a XIAO ESP32-S3 against vectors generated from `hear/`, with no microphone, GPS or
radio attached. The hardware it will run on is not settled: `docs/node-hardware.md` is the sensor bill of materials, `docs/faketec-pin-budget.md`
is a closed route kept for why it closed.

    python -m pytest tests -q

The field captures are not redistributable. Point `DAMA_HEAR_REAL_WAV` at your own single-channel
WAV to run the recorded-audio test.

## CI and releases

Every push to `main` and every pull request runs `.github/workflows/ci.yml` on GitHub-hosted
runners:

- the test suite twice: Python 3.12 with `requirements/ci-dev.txt`, and Python 3.13 with
  `requirements/ci-pods.txt`, which pins what the k3s pods install;
- the firmware generators (`firmware/gen_*.py`), failing if any committed header differs from
  what they write;
- every sketch under `firmware/` compiled with esp32 core 3.3.11 and `-DHEAR_ALLOW_NO_WIFI`, plus a
  build of `hear_node` and `puc_node` without it that must fail on the Wi-Fi guard. The images are
  kept as workflow artifacts for 14 days.

Every push to any branch and every pull request also runs `.github/workflows/coord-guard.yml`.
This repo is public and the site's real position must never be in it, so `tools/coord_guard.py`
fails on any decimal latitude/longitude pair, or `lat`/`lon`-keyed value, with at least four
decimal places that lies more than 25 km from `survey.json`'s fictional origin. It reads:

- a pair separated by `,`, `;` or whitespace, or a two- or three-element `[lat, lon]` array, with
  a `-` or `+` sign on either number;
- a `lat`/`latitude`/`lon`/`longitude`/`lng`-keyed value, alone or paired within three lines,
  with a `.` or a decimal comma (`DD,DDDD`);
- an ISO 6709 string: two explicitly signed numbers glued together (`+DD.DDDD-DDD.DDDD`), a `+`
  straight after the first number's last digit included. A trailing `/` (after an optional
  altitude and CRS), or a two-digit latitude with a three-digit longitude integer part, is enough
  on its own. Without either, both numbers must be at least 1, the first sign must not be glued
  to a letter, and an en dash or hyphen is not a sign. A number followed by `i` or `j` is a
  complex number, never ISO 6709;
- one uppercase latitude letter (`N`/`S`) and one longitude letter (`E`/`W`), with a `.` or a
  decimal comma: after each number (`DD.DDDDN, DDD.DDDDW`, with or without `°` and spaces, or
  glued as `DD.DDDDNDDD.DDDDW`) or before it (`NDD.DDDD EDDD.DDDD`, `NDD.DDDDWDDD.DDDD`). The letter
  gives the sign; an explicit sign that disagrees with it is checked both ways;
- separator escapes: `%2C`/`%3B` under up to three extra `%25` layers, and `%20` or `+` only
  directly after a comma or one of those escapes.

A sub-degree pair (both axes under 1) needs a key, both hemisphere letters, or the strict ISO 6709
shape, so a bare config array or inline pair like `[0.25, 0.75]` still passes. Not read, on
purpose: `_` and `|` as separators (they would flag filenames such as `model_a_b.pt` and markdown
or pipe-delimited float tables, and cannot catch a positive second number without doing so); `/`
(ratios and fractions in prose); lowercase or single hemisphere letters; a decimal-comma pair with
only a separator between (semicolon CSV, TSV, SVG points); `%20` anywhere but after a separator.
Known false positives, which the allowlist below handles: a hyphenated range followed by a number
(`a-b, c` pairs `-b` with `c`, because skipping it would also skip a glued negative latitude), and
values that happen to carry `N`/`S` next to `E`/`W`. It checks the tree and also every file each incoming commit
adds or changes. A commit that adds a coordinate and a later one that removes it still fails,
because the history is published too. It prints commit, path and line, never the value. Build test
coordinates as offsets from the fictional origin. The real origin comes only from
`HEAR_SITE_ORIGIN`. `tools/coord_guard_allow.txt` (`path digest  # reason`) is for numbers that
are not coordinates at all. Take the digest from a local `--show-digests` run, never from CI.
Every file under a couple of megabytes is scanned as text, binary-looking content included, so a
stray non-text byte in front of a coordinate can't hide it. A blob over that size is never
scanned, and the run fails over it, naming the commit and path but never its content, rather than
passing on incomplete coverage.

Pushing a `v*` tag runs `.github/workflows/release.yml`, which publishes the `hear_node` images
for each supported board class, their `.elf`, `build-info.json` and `SHA256SUMS` as a GitHub
release. This repo is public, so the images carry no Wi-Fi credentials and no node name. Each
node keeps its own in NVS, written once over USB by `firmware/hear_node/enroll.py`, and
`firmware/hear_node/flash.py <node> <ip> --release <tag>` refuses unless it can match the node's
live `/status class` to the right release asset. See `firmware/hear_node/README.md`.

## Licence

GPL-3.0. See `LICENSE`.
