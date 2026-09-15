# Cost and capacity model

This is a sizing model, not a deployment promise. The core assumption is still the architecture
in `docs/architecture.md`: nodes send timestamps, sketches and health, not continuous audio.
Audio is a separate, expensive mode for service visits, labelling windows or sites with real
backhaul.

## Recalculate it

Default variables:

| symbol | default | meaning |
|---|---:|---|
| `N` | fleet size | node count |
| `E_h` | 5 / node / hour | gated acoustic events shipped by each node |
| `T_s` | 30 s | telemetry period |
| `S_event` | 172 B | application sketch payload from `docs/uplink.md` |
| `S_event_store` | 1.3 kB | scored event row, including model identity and refusal accounting |
| `S_telemetry` | 14 B | node telemetry payload from `docs/node-hardware.md` |
| `S_telemetry_store` | 300 B | indexed database row after JSON/protocol/labels |
| `C_clip` | 0.14 | uncertain-band clip fraction from the labelling loop |
| `S_clip` | 480,044 B | one 48 kHz mono WAV clip currently budgeted by node firmware |
| `S_scene_day` | 9.45 MB / node / day | compressed 1.024 s scene rows, from 112 B/row |
| `S_pcm16` | 32,000 B/s | continuous 16 kHz 16-bit mono PCM |
| `S_pcm48` | 96,000 B/s | continuous 48 kHz 16-bit mono PCM |

Use decimal bytes for capacity planning unless a storage system reports otherwise.

Formulas:

```
events_per_node_day = E_h * 24
telemetry_rows_per_node_day = 86400 / T_s

wire_event_Bps = N * E_h * S_event / 3600
wire_telemetry_Bps = N * S_telemetry / T_s
messages_per_second = N * (E_h / 3600 + 1 / T_s)

db_B_day = N * (
  events_per_node_day * S_event_store +
  telemetry_rows_per_node_day * S_telemetry_store
)

selected_clip_B_day = N * events_per_node_day * C_clip * S_clip
scene_B_day = N * S_scene_day
continuous_audio_B_day = N * sample_rate_hz * 2 * 86400

retained_B = daily_B * retention_days * replication_or_erasure_overhead
```

For event-heavy deployments, multiply every event-derived line by `E_h / 5`. A 50 events/hour
bioacoustic gate is 10x the event, clip and sketch traffic shown here; it is not 10x the
telemetry or scene traffic.

## Nominal fleet sizes

Default rate: 5 gated events/node/hour, one telemetry row every 30 s, and 14 % of events selected
for clips. "Base ingress" is sketches plus telemetry only; it deliberately excludes clips, scene
rows and continuous audio.

| nodes | messages/s avg | 10x burst messages/s | base wire/day | database/day | selected clips/day | scene/day |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 0.35 | 3.5 | 0.61 MB | 10 MB | 81 MB | 95 MB |
| 100 | 3.5 | 35 | 6.1 MB | 102 MB | 806 MB | 945 MB |
| 1,000 | 35 | 350 | 61 MB | 1.02 GB | 8.1 GB | 9.45 GB |
| 10,000 | 350 | 3,500 | 610 MB | 10.2 GB | 80.6 GB | 94.5 GB |

At these rates, message count matters before bytes do. The 10,000-node base fleet is only
single-digit kB/s of raw payload, but it is hundreds of messages per second all day and thousands
per second during correlated bursts.

## Optional continuous audio

Continuous audio is the mode that changes the architecture. It should not share the same mesh
budget or retention assumption as sketches.

| nodes | 16 kHz PCM/day | 48 kHz PCM/day | 30 d at 48 kHz | 365 d at 48 kHz |
|---:|---:|---:|---:|---:|
| 10 | 27.6 GB | 82.9 GB | 2.5 TB | 30 TB |
| 100 | 276 GB | 829 GB | 25 TB | 303 TB |
| 1,000 | 2.76 TB | 8.29 TB | 249 TB | 3.0 PB |
| 10,000 | 27.6 TB | 82.9 TB | 2.5 PB | 30 PB |

If continuous audio is required, assume site-local buffering and bulk transfer. A central
real-time stream from 10,000 nodes is 7.7 Gbit/s at 48 kHz before TLS, framing, retransmits or
replication.

## Storage with retention and redundancy

Recommended storage classes:

| data | default retention | storage target | redundancy assumption |
|---|---:|---|---|
| telemetry and event indexes | 13 months hot, aggregate forever | PostgreSQL/Timescale or ClickHouse | 2 replicas at small scale, 3 at large scale |
| scored rows and refusal accounting | 13 months hot | object store plus indexed summary | 2 replicas or erasure coding |
| selected clips | 90 days hot, curated labels retained | S3/MinIO object store | erasure coding above one server |
| scene rows | 30-180 days hot | compressed object partitions | erasure coding above one server |
| continuous audio | explicit campaign retention only | site-local object store first | erasure coding; never 3x at 10,000 nodes |

Nominal 10,000-node hot growth without continuous audio:

| class | daily logical | 30 d logical | with 1.5x erasure overhead |
|---|---:|---:|---:|
| database rows | 10 GB | 306 GB | 459 GB |
| selected clips | 81 GB | 2.4 TB | 3.6 TB |
| scene rows | 95 GB | 2.8 TB | 4.3 TB |
| combined | 186 GB | 5.6 TB | 8.4 TB |

The database should not be the clip store. Keep clip and scene bytes in object storage and keep
only object keys, time ranges, labels and derived scores in the relational/OLAP database.

## Compute

Edge compute stays small: `docs/uplink.md` measures the 20x8 sketch at about 0.5 ms on a 64 MHz
Cortex-M4F per detection, not continuously. Central sketch scoring is also small; event volume at
the default rate is:

| nodes | events/s avg | selected clips/s avg |
|---:|---:|---:|
| 10 | 0.014 | 0.002 |
| 100 | 0.14 | 0.019 |
| 1,000 | 1.4 | 0.19 |
| 10,000 | 14 | 1.9 |

Practical self-hosted compute:

| nodes | CPU | GPU |
|---:|---|---|
| 10 | 2-4 vCPU mini PC or small VM | none |
| 100 | 4-8 vCPU, local SSD, nightly compaction | none unless tagging many clips |
| 1,000 | 3 small app/worker nodes, 16-32 total vCPU, separate database storage | optional single low-end GPU for batch audio tags |
| 10,000 | 6-12 app/worker nodes, 64-160 total vCPU, separate broker/database/object tiers | 1-4 datacenter GPUs if running neural clip/audio models continuously |

The GPU requirement is driven by clip or continuous-audio models, not by the shipped sketch path.
At 10,000 nodes the default clip queue is about 1.9 five-second clips/s on average. If a model
costs one realtime second per clip on CPU, that is roughly 10 busy CPU cores before burst margin;
if it costs 10x realtime, use GPUs or accept batch latency.

## Backhaul, node cost and power

Base telemetry/sketch traffic is compatible with low-rate radios. Clips, scene rows and audio are
not.

Per-node hardware planning ranges:

| item | low | rugged/field |
|---|---:|---:|
| MCU, storage and carrier | $12-45 | $40-120 |
| GPS with PPS | $8-30 | $20-60 |
| microphone and environmental sensor | $10-25 | $25-80 |
| radio/backhaul share | $8-40 | $30-150 |
| enclosure, cable, mount | $10-50 | $50-200 |
| battery and solar | $20-80 | $100-500 |
| **node BOM** | **$70-270** | **$265-1,110** |

Power planning ranges:

| node mode | average power | energy/day | 3-day battery before reserve |
|---|---:|---:|---:|
| MCU + mic + PPS, sparse radio | 0.2-0.6 W | 5-14 Wh | 15-43 Wh |
| ESP32-class WiFi service window | 0.7-1.5 W | 17-36 Wh | 50-108 Wh |
| continuous audio upload gateway/client | 1.5-4 W | 36-96 Wh | 108-288 Wh |

Backhaul rules of thumb:

| traffic mode | average per node | gateway guidance |
|---|---:|---|
| sketches + telemetry | <0.1 MB/day raw payload | one gateway can aggregate many hundreds if RF duty cycle allows |
| plus selected clips | ~8 MB/day | budget LTE/satellite by site; 100 nodes is ~24 GB/month |
| plus scene rows | ~18 MB/day | 100 nodes is ~54 GB/month |
| 48 kHz continuous audio | ~83 GB/day | needs wired/WiFi-class local network, not LoRa |

Use gateways by geography and RF airtime, not by byte count. A practical field gateway usually
serves 10-200 nodes depending on terrain, duty-cycle rules, retry rate and whether clip drains use
WiFi instead of the mesh.

## Operating cost ranges

These are self-hosted cash costs for servers, disks, backup media, power, network and gateway
data plans. They exclude staff time and field truck rolls, which dominate at large node counts.

| nodes | central infrastructure/month | gateway/backhaul/month | node maintenance/month | total/month before staff |
|---:|---:|---:|---:|---:|
| 10 | $20-150 | $0-80 | $10-100 | $30-330 |
| 100 | $150-700 | $50-800 | $100-1,000 | $300-2,500 |
| 1,000 | $1,500-8,000 | $500-8,000 | $1,000-10,000 | $3,000-26,000 |
| 10,000 | $15,000-80,000 | $5,000-80,000 | $10,000-100,000 | $30,000-260,000 |

Continuous audio changes the storage and network line items by one to two orders of magnitude.
For 10,000 nodes at 48 kHz, 30 days of logical audio is 2.5 PB before redundancy; that is a
storage project, not a flag on the sketch pipeline.

## Scaling thresholds

Scale by observed burst rate, retention growth and operational blast radius:

| threshold | keep | change |
|---|---|---|
| up to 100 nodes or <10 messages/s burst | single host, Postgres, local MinIO/filesystem, cron workers | take tested backups; do not add Kafka |
| 100-1,000 nodes or 10-500 messages/s burst | 2-3 app workers, managed process supervisor or k3s, Postgres with time partitions, object store | split clips/scene from database; add queue durability |
| 1,000-5,000 nodes or 500-2,000 messages/s burst | HA MQTT/NATS, 3 broker nodes, separate database and object-store hosts, autoscaled workers | partition by site/day/node; add idempotent ingest and replay |
| 5,000-10,000+ nodes or >2,000 messages/s burst | sharded ingest by region/site, ClickHouse/Timescale/Citus depending query shape, erasure-coded object store, dedicated GPU batch lane | multi-rack or multi-AZ redundancy; run load tests from recorded burst traces |
| any fleet with continuous audio | site-local audio store and scheduled upload | do not route raw audio through the LoRa/telemetry broker |

Do not scale the architecture just because the average byte rate looks small. Scale when retries,
burst fan-in, storage compaction, backup windows or one-host maintenance become the limiting
factor.
