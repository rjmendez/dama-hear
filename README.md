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

`tools/estimate_geometry_from_claps.py` is an **experimental** self-calibration probe: it tries
to infer relative node geometry and clap positions from clap TDOA alone. It is for feasibility
work only; production calibration still depends on surveyed geometry.

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
decimal places that lies more than 25 km from `survey.json`'s fictional origin.

Before reading, Unicode spaces (no-break, figure, thin, narrow no-break) count as a space, `º` and
`˚` as `°`, and the entities `&nbsp;`, `&#44;`, `&#x2C;`, `&comma;` and `&semi;` are decoded. It
reads:

- a pair separated by `,`, `;` or whitespace, or a two- or three-element `[lat, lon]` array, with
  a `-` or `+` sign on either number;
- a keyed value, alone or paired within three lines, with a `.` or a decimal comma (`DD,DDDD`).
  Keys are `lat`, `latitude`, `latitud`, `lon`, `long`, `longitude`, `longitud` and `lng`, also
  glued after `gps` or after a lowercase letter in camelCase (`homeLat`, `siteLongitude`). The key
  ends at a non-letter and is followed by `:`, `=`, `:=`, `=>`, `(`, an XML `>` (`<lat>`),
  `(deg)`, or whitespace alone;
- an ISO 6709 string: two explicitly signed numbers glued together (`+DD.DDDD-DDD.DDDD`),
  including a `+` straight after the first number's last digit and `%2B`/`%2D`/`%2F` escapes. The
  first sign never follows a digit or `.`, and an en dash or hyphen is never a sign. A `/`
  terminator is enough on its own: it may follow a decimal altitude, or any altitude before a
  CRS, and must itself be followed by the end of the text, whitespace, a quote, a closing
  bracket, `,`, `;`, `<` or a sign. Without it, the first sign must not follow a letter, `_` or a
  closing bracket, and either both numbers are at least 1 or the integer parts have two
  (latitude) and three (longitude) digits. A number followed by `i` or `j` is a complex number,
  never ISO 6709;
- one uppercase latitude letter (`N`/`S`) and one longitude letter (`E`/`W`), in either order,
  with a `.` or a decimal comma on each number: after each number (`DD.DDDDN, DDD.DDDDW`, with or
  without a degree sign and spaces, or glued as `DD.DDDDNDDD.DDDDW`), before each
  (`NDD.DDDD EDDD.DDDD`, `NDD.DDDDWDDD.DDDD`), or after the first and before the second
  (`DD.DDDDN WDDD.DDDD`). The letter gives the sign; an explicit sign that disagrees with it is
  checked both ways;
- separator escapes: `%2C`/`%3B` under up to three extra `%25` layers, with `%20`, `%09`, `%2B` or
  a form `+` read as a space only directly after one of them (`%20`/`%09` also after a literal
  comma).

A sub-degree pair (both axes under 1) needs a key, both hemisphere letters, or the ISO 6709 shape
above, so a bare config array or inline pair like `[0.25, 0.75]` still passes. Lowercase `n`, `s`,
`e` and `w` are tolerated as separator text, as on `main`, so `DD.DDDD n, DDD.DDDD w` still fails
when both values are at least 1; a lowercase letter never creates coordinate context and never
lifts the 1.0 floor.

Not read, on purpose: `_` and `|` as separators (they would flag filenames such as `model_a_b.pt`
and markdown or pipe-delimited float tables, and cannot catch a positive second number without
doing so); `/` (ratios and fractions in prose); lowercase or single hemisphere letters; a
decimal-comma pair with only a separator between (semicolon CSV, TSV, SVG points); `%20` anywhere
but after a separator; an en dash as an ISO 6709 sign (it is range punctuation); `Breite`/`Laenge`
as keys (they are the ordinary German words for width and length).

Digests of hemisphere pairs ignore the letters, so allowlisting a plain `a, b` pair in a path
also allows every hemisphere-letter spelling of the same two magnitudes in that path. A
decimal-comma keyed partner within three lines can turn a lone keyed finding into a pair, which
changes its digest and its reported line.

Known false positives, which the allowlist below handles:

- a hyphenated range followed by a number (`a-b, c` pairs `-b` with `c`, because skipping it would
  also skip a glued negative latitude);
- values that happen to carry `N`/`S` next to `E`/`W`, including sub-degree unit tables that use
  both letters (newtons or siemens next to watts);
- decimal-comma tuples after a key, and `long` in prose followed by a decimal-comma number;
- tolerances and offsets written as `(+a-b)` with both values at least 1.

It checks the tree and also every file each incoming commit
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
for each supported board class, their `.elf`, `build-info.json`, `release-manifest.json`,
`release-manifest.schema.json` and `SHA256SUMS` as a GitHub release. This repo is public, so the
images carry no Wi-Fi credentials and no node name. Each node keeps its own in NVS, written once
over USB by `firmware/hear_node/enroll.py`, and
`firmware/hear_node/flash.py <node> <ip> --release <tag>` refuses unless it can match the node's
live `/status class` to the right release asset. Downloaded release directories can be checked
offline with `python3 firmware/hear_node/release_manifest.py verify --dist <dir> --tag <tag>`.
See `firmware/hear_node/README.md`.

## Licence

GPL-3.0. See `LICENSE`.
