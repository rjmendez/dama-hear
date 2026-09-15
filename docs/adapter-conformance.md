# Adapter conformance suite and the recoupling boundary

`docs/standalone-migration.md` states the independence gate as a sentence: "a clean-room install
with only the core, one node adapter, local durable storage and test doubles passes this suite and
replays an offline SD backlog". This page is where that sentence is executable.

Two files, one command each, both merge-blocking:

| what | where | run |
| --- | --- | --- |
| Seven-case adapter conformance suite | `tests/test_adapter_conformance.py` | `python -m pytest -q tests/test_adapter_conformance.py` |
| Recoupling import boundary | `tools/recoupling_guard.py`, `tests/test_recoupling_guard.py` | `python tools/recoupling_guard.py` |

CI runs both in the `adapter conformance and recoupling boundary` job of `.github/workflows/ci.yml`,
and the whole pytest job runs them again — turning the named job off does not disarm the gate.

## Why the suite is parametrised rather than written twice

The repository already has good per-function tests. What it did not have is one place asserting
that **every** ingress answers the same failure the same way. `ADAPTERS` holds the two node
ingresses that exist today behind one `write()` signature:

* `dets-csv` — a card-backed XIAO's `dets.csv`, at generation G7.
* `live-ring` — a cardless ESP32's `/detections`, in the `cursor-v1` envelope.

Every case below runs against both. A third adapter — the phase 4 HTTPS batch push — becomes one
entry in that tuple and inherits all of it on the day it lands, which is the point: a new transport
cannot be merged half-conformant, because there is nowhere to put it that skips these.

No live service is touched. Frames are packed with `hear.wire`, bodies are written under pytest's
`tmp_path`, and the drain is exercised through its own pure helpers.

## The seven cases

1. **Stale anchors.** A XIAO latches `time_valid` at its first fix and never clears it, so a node
   whose GPS UART died keeps stamping while its crystal free-runs at a measured 4.2–11.7 ppm — about
   30 ms/h, invisible in every other counter it exports. The suite asserts the stated
   `sync_sigma_ns`, `clock_state` and `anchor_age_us` survive ingest unaltered; that the class gate
   (`hear.nodeclass.stamp_admissible`) refuses the free-running one and admits a fresh one; that a
   stated `0` reads as *unstated* and never as a perfect clock; and that an **absent** statement
   stays usable, so adding the column cannot retroactively refuse every row that predates it.
2. **Reader laps and torn copies.** A fetch that raced the node's writer, and a ring read after it
   lapped. A torn row must be a counted refusal with a reason and the whole rows must still land;
   `rows == added + duplicate + skipped` must close; an empty body is an error and not a quiet
   period; a truncated page carries the cursor only as far as its contiguous prefix; a lapped ring
   reports the rows the pool will never see; and unmeasured is `None`, never `0`.
3. **Timestamp fallback.** `utc_us == 0` is kept, marked unanchored, filed under `unanchored`
   rather than a guessed day, and keeps `pps_n`/`us_since_pps` so the row stays recoverable. A
   node row does not answer the phone's `utc_trusted` question at all; a phone's `wall` tier is
   anchored and **not** trusted; an unrecognised tier is refused rather than assumed good.
4. **Missing sample-rate / profile metadata.** Profile 0 states no rate, so a record built from one
   says `fs_hz = None` and `fs_stated_by = None` rather than defaulting. A rate the column states is
   attributed to the column; a rate the frame states outranks it and the column's value stays
   visible. An unknown profile id raises, and profile 0 is not selectable for a new frame.
5. **Transport loss, retry and idempotency.** Every delivery here is at-least-once. The same body
   twice adds one event; an overlapping retry adds only what is new; both attempts are ledgered with
   the bytes they carried, so "fetched twice" and "fetched once" stay distinguishable; deliveries
   converge whatever order they land in; the cursor watermark never moves backwards inside a boot;
   a malformed cursor is refused rather than coerced to `0` or "latest"; and a cursor belongs to the
   boot that minted it.
6. **Cross-profile event identity.** One detection drained off a card and out of a live ring lands
   **once** — if the transports keyed it differently the corpus would hold one event twice — and it
   lands with the *same stored schema*, although the card writes these scalars as CSV text and the
   ring sends them as JSON numbers. Two frames that differ only in what their bytes *mean*
   (profiles 1 and 2 share a shape and disagree about the rate) stay two events. The claimed profile
   is carried into the record. And a row cannot be filed under a node the fetch disagrees with.
7. **Recoupling import boundary.** Below.

## The recoupling boundary

`docs/standalone-migration.md` lists these as merge-blocking: "imports from gotchi/cloud/deployment
packages into `hear/` or `modules/`; core code naming AWS, Oxalis, Redis keys, PVC paths, Android
classes or `gotchi-phone`; direct core writes to Redis/MQTT/PVC".

`tools/recoupling_guard.py` enforces exactly that over `hear/` and `modules/`. Rules and the
sentence behind each: `python tools/recoupling_guard.py --list-rules`.

Two deliberate choices about what it will and will not chase. `importlib.import_module("redis")`
and `__import__("boto3")` **are** caught, because the repository already contains the shape that
drifts into them — a function-local fail-open import inside a `try`. A module name assembled from
concatenated pieces is **not** caught, because that is deliberate evasion rather than drift and a
checker that tried would be guessing. A scan root that does not exist is a failure, not an empty
one: `os.walk` on a missing path raises nothing, so a renamed package would otherwise leave the
gate exiting 0 having inspected nothing.

**It parses, it does not grep.** `hear/nodeclass.py` cites dama-gotchi's own calibration files in
twenty comments and `hear/corpus.py` quotes an Android source line, because that is where those
numbers came from. A text scan calls every one of them a dependency and the gate gets switched off
within a week. The guard walks the AST and looks at exactly two things — what a file **imports**,
and the string literals it **evaluates**. A citation in a comment or a docstring is free; a
coupling is not.

### The allowlist is scoped to a value, never to a file

`tools/recoupling_allow.txt` holds one line per `path :: rule :: exact token :: reason`. Allowing a
whole file would let the *next* PVC path into the same module unnoticed, which is the failure the
gate exists to stop. An entry that stops matching anything is itself an error, so the list cannot
rot into a blanket exemption.

Three couplings shipped before the gate existed and are recorded rather than edited, because moving
where a running service publishes or writes is a rollout with a rollback plan and not a lint fix:

| file | rule | why it is recorded |
| --- | --- | --- |
| `hear/nodeclass.py` | `android_gotchi` | `gotchi-phone` is the registered name of a producer **class** in the timing taxonomy. Nothing imports, dials or requires dama-gotchi; deleting the adapter leaves the class unused, not broken. |
| `hear/spatial.py` | `pvc_path` | `append_geojsonl()`'s shipped default, already overridable by `SPATIAL_GEOJSONL_PATH`. Its consumers are live, so the path moves in the phase 3 object-store import with a dual-read window. |
| `hear/spatial.py` | `mqtt` | Function-local, fail-open `publish_mqtt_event()`. The import sits inside the `try` and its failure returns `False`, so no import graph and no solve depends on a broker. It moves behind `ResultSink.publish` in phase 4. |

What the gate buys today is that a **second** one cannot arrive silently.

## Adding an adapter

1. Add a writer and an `Adapter(...)` entry in `tests/test_adapter_conformance.py`. Every case runs
   against it immediately; expect the first run to be red.
2. Fix the adapter, not the case. A case that is genuinely inapplicable needs a stated reason in the
   test, in the shape of the existing per-case docstrings.
3. Keep the boundary green: the adapter itself lives outside `hear/` and `modules/`, and may depend
   on the core. The core may never depend on it.
