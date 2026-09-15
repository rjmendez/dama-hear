# Resilience

## Why this is one document, not ten

Every failure mode below is an instance of the same three questions: what is the invariant that
must survive, what does a node/service do when it cannot reach the thing it depends on, and how
does an operator learn about it without reading a stack trace. Answering those three per failure
mode, once, is cheaper than discovering each the way `hear_drain.py`'s header comments show this
project already discovered several of them in production.

## Invariants (must hold under every failure mode below)

1. **A node's own timestamp is authoritative and never rewritten downstream.** The network
   solves geometry from independently-disciplined clocks (`docs/architecture.md`); no recovery
   path may re-timestamp an event against a central clock, a retry time, or a receipt time.
2. **Raw bytes are archived before they are parsed, and parsing is re-runnable.** A parser bug is
   assumed, not excluded (`hear_drain.py`'s `raw/<node>/<utc>-<file>`); state derived from a raw
   file must always be reconstructable by re-running the parser, never by re-fetching.
3. **Every write is idempotent and content-addressed where it can be.** Re-delivery, overlapping
   drain runs, and reflashes must cost nothing, not corrupt state.
4. **A component that cannot do its job fails loud, not quiet.** Staleness, not a thrown
   exception, is the default failure shape of a sensor network (`hear_drain.py`: "a drainer whose
   HTTP succeeds and whose node has stopped detecting exits 0 forever"). Every degraded state
   must be observable within one heartbeat interval.
5. **A node's backlog is bounded by construction, not by hope.** Anything that grows without
   bound on a device with finite flash (`dets.csv`) must have a rolling window, an eviction
   policy, or both, sized against a measured write rate, not an assumed one.
6. **Identity is checked, never assumed.** DHCP moves, boards get re-flashed, node names get
   re-used; anything filed under a node name must first confirm that name against a live check.
7. **Degradation is per-node and per-stage.** One bad lease, one corrupt file, one stalled
   pipeline stage must not stop the fleet or the pipeline; failure is isolated to its source.

---

## 1. Network partitions (mesh split, backend unreachable)

**Where it happens:** LoRa/Meshtastic hops between nodes and the gateway; the gateway-to-k8s
uplink; k8s pipeline stage-to-stage (`hear-mqtt-bridge` → `hear-tdoa` → `hear-score` → ...).

**Invariant at risk:** none of the core invariants require the network — that is the whole point
of PPS-disciplined local timestamping (`docs/architecture.md`: "the network never has to agree on
time"). A partition must degrade *latency of visibility*, never *correctness of the eventual
record*.

**Policy:** a node buffers detections and sketches locally (bounded, see §5) and re-sends on
mesh reconnect; it never blocks detection on uplink success. A pipeline stage on a stalled queue
(e.g. `hear-mqtt-bridge` unreachable) holds its unread messages at the broker/queue, not in
process memory, so a restart does not lose them. No stage retries into the partition
indefinitely — it backs off and reports itself stale (§ operator visibility) rather than spinning.

**State machine (node uplink):**
```
CONNECTED --(N missed acks)--> DEGRADED --(mesh silent > T_partition)--> PARTITIONED
PARTITIONED --(heartbeat ack seen)--> CONNECTED
```
`PARTITIONED` does not stop detection or local logging; it only stops trying to push and starts
accumulating for the next drain/mesh-recovery cycle.

**Operator visibility:** heartbeat TTL keys in Redis (`HEAR_HEARTBEAT_TTL_S=30`) expire when a
node stops phoning home; `hear_drain.py --check` fails loud if the last *successful* fetch per
node exceeds `--max-stale-s`. A partition of N nodes shows up as N expired keys, not as a single
alarm, so the operator can see the shape of the split (one node vs. the whole fleet vs. the
gateway itself).

**Chaos test:** kill the gateway↔k8s link for 2×, 10×, and 60× the drain interval; assert (a) no
node stops detecting or logging locally, (b) `--check` fails within one heartbeat TTL of the cut,
(c) all buffered detections appear in the pool after reconnect with no duplicates and no gaps
larger than the buffer's bound.

---

## 2. Intermittent nodes (flapping mesh link, marginal RF)

**Invariant at risk:** #7 (isolation) and #4 (loud staleness) — a flapping node is the case most
likely to be mistaken for "fine" because it succeeds often enough to reset alarms.

**Policy:** liveness is TTL-based, not edge-triggered — a node that flaps is a node whose Redis
key keeps almost-expiring, and the drain's staleness check must key off *last successful fetch*,
not *last attempt*, exactly as `hear_drain.py` already documents. Flap rate itself is a
first-class signal: count reconnects per hour per node and surface it, because a node flapping
10×/hour is a different maintenance ticket than one that dropped once.

**State machine:** same `CONNECTED / DEGRADED / PARTITIONED` machine as §1, but flap count is
tracked across transitions rather than reset on each `CONNECTED`, so a node bouncing between
states never reads as healthy in the aggregate.

**Operator visibility:** a per-node flap counter (windowed, e.g. transitions/hour) alongside the
heartbeat TTL, so "down" and "flaky" are visually distinct states, not both collapsed into "seen
recently."

**Chaos test:** synthetic 50% packet loss on one node's mesh link for an hour; assert the flap
counter rises, `--check` does not oscillate pass/fail on each individual heartbeat, and the
node's data still lands in the pool deduplicated and gap-annotated.

---

## 3. Reboot / brownout (node power loss, ESP32 brownout detector, k8s pod restart)

**Invariant at risk:** #1 (timestamp authority — a reboot must not touch the PPS discipline
model retroactively) and #5 (bounded backlog — `dets.csv` rolling to `dets-prev.csv` on reflash
is the existing mechanism; it must not be silently widened).

**Policy:** on boot, a node re-acquires GPS lock before it timestamps anything as PPS-disciplined
(flagged in telemetry via `pps_locked`); detections before lock are timestamped against local
oscillator time and tagged unlocked, never silently upgraded to "disciplined." The current file
(`dets.csv`) rolls to `-prev` rather than truncating, so a reboot loses at most one file's worth,
bounded and already fetched-both by the drain. A k8s pod restart must resume from durable queue
state (broker offsets / PVC), never from in-memory state.

**State machine (node boot):**
```
BOOT -> GPS_ACQUIRING -> (timeout) -> RUNNING_UNLOCKED
GPS_ACQUIRING -> (PPS lock) -> RUNNING_LOCKED
RUNNING_UNLOCKED -> (PPS lock) -> RUNNING_LOCKED
RUNNING_LOCKED -> (lock lost, e.g. §8) -> RUNNING_UNLOCKED
```

**Operator visibility:** `pps_locked` bit is in every telemetry frame (`telemetry.py: pack(...,
pps_locked, ...)`); the backend surfaces "unlocked" detections distinctly (excluded from TDoA
solves by default, included only in an explicitly-flagged analysis). Reboot count and last-boot
timestamp per node are part of the heartbeat, so a node brownout-looping shows up as a rising
reboot counter, not just a gap.

**Chaos test:** power-cycle a node mid-detection; assert (a) the in-flight event is either fully
recorded or fully absent, never truncated, (b) the node comes back and correctly tags
pre-lock detections as unlocked, (c) `-prev` roll is fetched by the next drain with zero loss for
gaps under one boot cycle; separately, kill a k8s pipeline pod under load and assert no message
is acked-and-lost (consumer offset only commits after durable write).

---

## 4. Storage exhaustion (node flash full, PVC full, Redis OOM)

**Invariant at risk:** #5 (bounded backlog) and #4 (loud, not quiet).

**Policy:** node-side, clips are already bounded by a measured rolling cache
(`--clip-max-per-node 96`, backed by a 128-clip firmware FIFO) — the fix for exhaustion is
eviction-with-counting, not silent overwrite: every eviction refusal is counted by reason
(`hear_drain.py`) so "we are losing clips to the cap" is a number, not a guess. The unbounded
case (`scene.csv`, which "grows without bound") is the one that must be fixed at the source
(rotate/cap it) rather than patched at the drain. PVC exhaustion on the backend (`hear-pool`)
degrades to refuse-new-writes-loud rather than corrupt-existing-data; Redis eviction policy for
heartbeat keys must be `volatile-ttl`/allkeys-lru scoped so liveness keys are never the data that
gets evicted to make room for something else.

**State machine (per-lane storage):**
```
OK -> (used > warn_pct) -> NEAR_FULL -> (used > hard_pct) -> REFUSING_WRITES
REFUSING_WRITES -> (space reclaimed, e.g. successful drain) -> OK
```

**Operator visibility:** `clips_deferred_by_cap` counter read off the heartbeat ring (already
named in the drain's own comments as the metric to watch for 7 days before raising a budget);
PVC usage alerting at NEAR_FULL, well before REFUSING_WRITES; a distinct alert class for "growing
without bound" files vs. "capped, evicting" files, since the operator response differs (rotate
the file vs. accept measured loss).

**Chaos test:** fill a node's flash to the clip cap under sustained detections; assert eviction
count increments, no write corrupts the rolling file, and drain still succeeds on what remains.
Fill the `hear-pool` PVC to 100%; assert the drain CronJob fails loud (non-zero exit, alerting)
rather than partially writing a corrupt corpus entry.

---

## 5. Duplicate delivery (mesh retry, re-drained file, overlapping CronJob runs)

**Invariant at risk:** #3 (idempotent, content-addressed writes).

**Policy:** already largely designed correctly and worth stating as policy rather than
incidental behavior: pool writes are content-addressed so re-fetching the same bytes is free
(`hear-drain`'s own annotation: "Content-addressed and idempotent, so overlapping runs and
re-fetched files cost nothing"); clip fetches are indexed by name in `clips/index.jsonl` so a
clip is "never asked for twice and a destroyed one is never re-probed"; scene rows are
deduplicated on ingest (60% duplicate rate is measured and expected, not an anomaly). The policy
generalizes: any at-least-once delivery path (mesh retry, CronJob `concurrencyPolicy: Forbid`
overrun, drain re-run) must land on a dedup key derived from content or from
(node, sequence/offset), never on wall-clock receipt order.

**State machine:** none needed — this is a property of the write path (dedup-on-ingest), not a
per-message lifecycle. The invariant is checked, not modeled as states.

**Operator visibility:** duplicate rate is already tracked (measured, e.g. "60.05% of every scene
row... was already a duplicate") — keep it as a first-class metric so a *sudden change* in
duplicate rate (near 0%, meaning something upstream stopped overlapping/retrying — see §11 tail
loss — or near 100%, meaning a retry storm) is the alarm, not the raw rate.

**Chaos test:** re-run the drain concurrently against the same node state; assert pool size and
detection count are unchanged from a single run. Force a mesh ack loss so a node retransmits the
same sketch 3×; assert exactly one event lands in the backend pipeline.

---

## 6. Corrupt frames / files (bit errors on air, truncated WAV, bad CSV parse)

**Invariant at risk:** #2 (raw archived before parsed) and #7 (isolation — one bad file must not
stop the drain).

**Policy:** every raw file is archived byte-for-byte before any parser touches it. A parse
failure marks that file/frame as `quarantined` (moved aside, counted, alerted) and the run
continues with everything else — mirrors the existing DHCP-mismatch handling ("A mismatch
refuses THAT node and the run continues with the others"). On-air frames carry a CRC (LoRa/
Meshtastic already does this at the radio layer); a frame failing CRC is dropped at the radio and
never reaches the parser, so corruption that matters to this design is the file-level kind:
truncated WAV on power loss mid-write, or a CSV row split across a flash-wear boundary. Recovery
is re-parse from the raw archive after a parser fix — never re-fetch, since the node may have
already rolled or evicted the source.

**State machine:**
```
FETCHED -> PARSING -> PARSED_OK
PARSING -> (parse error) -> QUARANTINED (counted, alerted, raw kept)
QUARANTINED -> (parser fixed, re-run) -> PARSED_OK | QUARANTINED (still bad)
```

**Operator visibility:** a quarantine count and list of quarantined files, distinct from the
staleness alert, since "the node is fine but this one file is bad" needs a different fix
(reparse) than "the node has gone dark" (§1/§2).

**Chaos test:** truncate a WAV mid-header and feed it through the parser; assert it is
quarantined, counted, and does not stop the run; assert re-parsing after a header-skip fix
recovers the file with no re-fetch. Feed a header-name-mismatched `dets.csv` (the historical bug:
"730 of its detections read as 0 rows under a header-name parser") and assert the discovery
mechanism (`/ls`-driven name discovery) or a schema-version check now catches it loud instead of
returning 0 rows silently.

---

## 7. Queue overload (burst of detections, pipeline backpressure, Redis under load)

**Invariant at risk:** #5 (bounded backlog) and #1 (timestamp authority — backpressure must delay
processing, never approximate or drop the original timestamp).

**Policy:** each k8s pipeline stage (`hear-mqtt-bridge` → `hear-tdoa` → `hear-score` → `hear-tag`
→ `hear-embed`/`hear-annotate`/`hear-birdnet`) is decoupled by a durable queue/broker so a slow
downstream stage applies backpressure upstream rather than dropping in-flight work; a stage
under sustained overload sheds load by *shedding low-priority classes of work* (e.g. deprioritize
`hear-birdnet`/embedding before dropping `hear-tdoa` inputs, since geometry solving is the
higher-value path) rather than dropping indiscriminately or crash-looping. On the node side, a
detection burst (e.g. a multi-round string) is exactly the case the wire format already optimizes
for (one sketch per string, delta-times for the rest) — the design already trades detail for
guaranteed delivery under burst, and that trade must not regress under load elsewhere in the
pipeline.

**State machine (per stage):**
```
NORMAL -> (queue depth > soft_limit) -> SHEDDING_LOW_PRIORITY
SHEDDING_LOW_PRIORITY -> (queue depth > hard_limit) -> BACKPRESSURING (upstream told to slow/buffer)
BACKPRESSURING -> (queue drained below soft_limit) -> NORMAL
```

**Operator visibility:** per-stage queue depth and shed-count metrics; an alert distinct from
"stage is down" for "stage is up but shedding," since the operator response (scale replicas,
raise budget, investigate a burst) differs from a crash response.

**Chaos test:** replay a recorded burst (10-round string × N nodes simultaneously) into
`hear-mqtt-bridge`; assert no stage crashes, low-priority work (birdnet/embed) is what gets
shed first if anything is, and `hear-tdoa` inputs are preserved or queued rather than dropped.

---

## 8. Clock / GNSS loss (PPS unlock, GPS outage, jamming/multipath)

**Invariant at risk:** #1 directly — this is the invariant the whole architecture is built to
protect (`docs/architecture.md`: nodes discipline to PPS at ~30 ns).

**Policy:** loss of PPS lock is a first-class, explicit state, not a silent fallback. A node that
loses lock keeps timestamping (detection must not stop) against its free-running oscillator, but
every event produced while unlocked is tagged unlocked and carries an estimated drift bound; the
backend TDoA solver excludes unlocked events from geometry solves by default (an unlocked
timestamp is worse than no timestamp for a technique that needs tens-of-nanosecond agreement) and
surfaces them separately for whatever degraded use is still valid (e.g. presence/absence, not
position). Reacquisition re-arms full trust only after the receiver reports a stable lock for a
minimum hold-down, to avoid flapping between locked/unlocked on marginal reception.

**State machine:** the `RUNNING_LOCKED / RUNNING_UNLOCKED` machine from §3, with drift-bound
growing monotonically with unlocked duration (used to size the exclusion/inclusion decision and
to bound how stale a "still roughly OK" claim can be).

**Operator visibility:** `pps_locked` and drift bound already ride in telemetry; the backend
should report *fleet-wide* lock health (e.g. "3 of 9 nodes unlocked") since correlated GNSS loss
across nodes (jamming, a bad antenna batch, a firmware regression — see §9) is a different
incident than one node's antenna coming loose.

**Chaos test:** simulate GPS antenna disconnect on one node for 10 minutes; assert detections
continue, are tagged unlocked, are excluded from TDoA solves, and drift bound grows monotonically;
on reconnect assert a hold-down period before the node is trusted as locked again. Simulate
correlated loss on all nodes simultaneously; assert the fleet-wide alert distinguishes this from
a single-node fault.

---

## 9. Firmware regression (bad flash, schema change, behavior change post-update)

**Invariant at risk:** #6 (identity checked, not assumed) and #4 (loud failure) — a regression
that silently changes wire format or detection behavior is the hardest to detect, since the node
still "works."

**Policy:** the existing release flow already refuses a mismatched flash target
(`flash.py <node> <ip> --release <tag>` "refuses unless it can match the node's live `/status
class` to the right release asset") — generalize that same check to every ingestion path: the
drain reads back `/status` before filing anything under a node's name, and any wire-format
version bump is carried in the frame itself (the `flags`/schema fields already present in
`telemetry.pack`/uplink frames) so the backend can detect and refuse to silently misparse an
old-format frame as new or vice versa. A regression that changes detection *behavior* rather than
*schema* is not automatically catchable by parsing — it is caught by monitoring detection-rate and
AUC-proxy metrics per node/board-class post-rollout and comparing to the pre-rollout baseline
(canary one board class before fleet-wide).

**State machine (rollout):**
```
STAGED -> CANARY (one board class) -> (metrics within bound, hold period) -> FLEET
CANARY -> (metrics regress) -> ROLLED_BACK
FLEET -> (post-rollout regression detected) -> ROLLED_BACK
```

**Operator visibility:** per-release `build-info.json`/`SHA256SUMS` already published; add a
per-node "running firmware version" surfaced in heartbeat/telemetry so a fleet mid-rollout is
visible, and a rollout dashboard comparing canary vs. fleet detection-rate/schema-version so a
regression is caught before it reaches every node.

**Chaos test:** flash a deliberately schema-incompatible build to a canary node; assert the drain
detects the version mismatch and quarantines its output rather than misparsing it; assert the
`--release` flash refusal fires correctly when `/status class` doesn't match the target asset.

---

## 10. Dependency outage (Redis down, PVC unavailable, upstream `audit-redis` unreachable)

**Invariant at risk:** #4 (loud, not quiet) — a heartbeat receiver that can't reach Redis must not
silently accept and drop liveness data; #7 (isolation) — one dependency outage must not take down
unrelated pipeline stages.

**Policy:** `hear-heartbeat`'s readiness/liveness probes already gate on `/healthz`; that probe
must itself check Redis reachability (with the existing `HEAR_HEARTBEAT_SOCKET_TIMEOUT_S=0.5`
bound) so a Redis outage takes the pod out of rotation rather than accepting writes it cannot
persist. Detection and local logging on nodes never depend on the backend being up at all — the
dependency-outage blast radius is *visibility*, never *data capture*, by construction (§1
invariant already covers this). Stages downstream of a failed dependency (e.g. `hear-tdoa` if its
input queue backend is down) should hold and retry with backoff, not drop their input.

**State machine:**
```
DEP_UP -> (probe fails) -> DEP_DOWN (pod unready, traffic not routed to it, upstream buffers)
DEP_DOWN -> (probe succeeds, N consecutive) -> DEP_UP
```

**Operator visibility:** k8s readiness state already surfaces this via normal pod status; ensure
an explicit alert on "hear-heartbeat unready" separate from "hear-heartbeat crashlooping," since
the former means "we can't see the fleet" (urgent, but nodes are fine) while the latter means "the
receiver itself is broken."

**Chaos test:** block `audit-redis` from `hear-heartbeat` for 5 minutes; assert `/healthz` goes
unready within the socket timeout bound, node detections continue unaffected, and no heartbeat
data is silently accepted and discarded during the outage.

---

## 11. Split brain (two drains/pipelines both believing they own the same node or file)

**Invariant at risk:** #3 (idempotent writes) and #6 (identity checked) together — split brain in
this system's shape is "two processes both think they're the authoritative drainer/solver for a
node or a time window."

**Policy:** the CronJob's own `concurrencyPolicy: Forbid` already prevents two drain runs
overlapping in the common case; the deeper protection is that drain writes are content-addressed
and idempotent regardless (§5), so even a `Forbid` violation (e.g. a stuck job plus a forced
re-run) cannot corrupt the pool — at worst it repeats work for free. For the TDoA solver, "split
brain" would mean two solves claiming authority over the same event window; this is avoided by
solves being derived, re-runnable, content-addressed against their input event set (same
principle as #2/#3) rather than mutating shared state in place — a re-solve is a new artifact,
not an in-place update racing another writer. The identity check ("mismatch refuses THAT node")
is what prevents split brain at the *node* level: two DHCP-swapped nodes never both write under
one name, because the name is verified against a live `/status` read, not assumed from
config.

**State machine:** not modeled as a lifecycle — prevented structurally (idempotent + derived +
identity-checked writes mean there's no shared mutable state for two owners to disagree about).

**Operator visibility:** drain run overlap is already loggable via CronJob job history
(`successfulJobsHistoryLimit`/`failedJobsHistoryLimit`); add an explicit counter for
"identity check refused a node" events, since a *rising* refusal rate is the leading indicator of
a DHCP/lease problem that would otherwise present as intermittent, hard-to-explain data
corruption.

**Chaos test:** force two drain jobs to run concurrently against the same node set (bypass
`Forbid` for the test); assert final pool state matches a single successful run. Swap DHCP leases
between two nodes mid-run; assert both nodes' data is refused/quarantined rather than filed under
swapped names, and the refusal is counted and visible.

---

## Chaos test harness (cross-cutting)

A single harness should be able to compose the above into scenarios, since real incidents are
rarely one failure mode in isolation (e.g. §1 partition + §4 storage exhaustion, if a partition
lasts long enough for the local buffer to fill). Minimum coverage:

- **Per-mode fault injection**: mesh drop, pod kill, disk-fill, GPS antenna disconnect, Redis
  block, corrupt-file injection, concurrent-job force-run, DHCP lease swap — each independently
  triggerable against a test fleet (physical or simulated nodes + a scratch k8s namespace).
- **Composite scenarios**: partition-until-buffer-full, reboot-during-partition,
  GNSS-loss-during-firmware-rollout.
- **Invariant assertions after every scenario**, not just "did it recover": no duplicate events in
  the pool, no re-timestamped events, no silently-dropped data outside a documented, counted
  eviction policy, every degraded state surfaced within one heartbeat interval, every quarantine
  and refusal counted.
- **Recovery-time measurement**: time from fault injection to (a) operator-visible alert and (b)
  full data recovery once the fault clears, tracked per mode so regressions in recovery time are
  caught the same way accuracy regressions are tracked in `docs/uplink.md`.
