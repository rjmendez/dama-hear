# Phase 0 contract freeze v1

Repository-visible baseline only. This inventory deliberately excludes live secrets, cluster reads, Redis contents, private corpus payloads, and raw fixture bodies.

## Reproduce

```bash
python3 tools/freeze_contracts.py --format json > docs/data/phase0-freeze-contracts.v1.json
python3 tools/freeze_contracts.py --format markdown > docs/phase0-freeze-contracts.v1.md
python3 tools/freeze_contracts.py --check
```

## Section hashes

| section | sha256 |
| --- | --- |
| baseline | 6f264345ddcfc40eed6b8fccd584954b250b8954ed79b01f49ca3f08c9db188b |
| wire_profiles | 4ea3d122ddf5d4f736624acd394fec2a82a8cec8550fc63c36f1af1e6502bbba |
| firmware_build_metadata | b39faa85108f494d66c2f81ab768d94261fa4ffeed08b1a0bda765c236c9805d |
| schemas | 427c86937d28bd12b6de71c3b4604eae609d2b24edfd4f1a761256bc74a6b580 |
| mqtt_topics | 800a4d7031d24e55b408d0c7b03a916924c6449dde75ae027126feac63494be8 |
| redis_keys | d7ca4d6fb110a1d3587adf6357413412444957728ae13cefe047279c85729a03 |
| kubernetes_and_pvc_layout | 849be220cbf001054dd939e25a023e245a0f01638b1f97a3c05659238bdd8edc |
| corpus_fixture_metadata | 5a27ffc150822a94847095b765d20e4f0299ec4d9e58f2ea1af05d6ecf470d77 |

## Compatibility

This freeze adds read-only tooling and versioned baseline artifacts only; it does not change wire formats, firmware behavior, MQTT topics, Redis keys, or manifests.

### Forward path

- Regenerate the JSON and markdown baselines before any contract change and diff them in review.
- Allocate new wire profile ids or schema generations instead of editing deployed meanings in place.
- Document transport, Redis, or manifest layout changes beside the code that changes them.

### Rollback path

- Revert the contract-changing commit or restore the earlier versioned baseline files.
- Redeploy the prior firmware tag or manifest set if an operational change shipped with the contract change.
- Run `python3 tools/freeze_contracts.py --check` to confirm the checkout matches the frozen baseline again.

## Wire profiles

| id | legacy | bands | frames | fs_hz | layout | wire bytes | profile hash |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | yes | 20 | 8 | None | nyquist | 173 | 8a7ba0284ffc4a541f284c1579627782b04d557758c0f7fce5580bf63e193c51 |
| 1 | no | 20 | 8 | 48000.0 | fixed | 173 | 61b0a0210c6b536c5a5b2bb467fc7a022cb46d3c09953e80717e3a230ea1ea07 |
| 2 | no | 20 | 8 | 16000.0 | fixed | 173 | 542cb37884b6303cdbb5dd290445b4a3a27714f6874cb410651d45b076d0b53a |
| 3 | no | 20 | 8 | 32000.0 | fixed | 173 | bcfa39189546c524585ae43e64125baf2e8324f8c472f086c7d82daefba9120f |
| 4 | no | 20 | 8 | 24000.0 | fixed | 173 | af8d6082d6380add519d82d9e45fb88cf39658cd655403f962b3b81a18bd72d8 |

Source hashes:

| path | size_bytes | sha256 |
| --- | --- | --- |
| firmware/hear_node/mel_impulse.h | 9147 | d56d54b4dbf83b95963bacff98d3b8d6306ba56fe068360a9024ddba8e79ea77 |
| firmware/hear_node/sketch_domain.h | 3264 | fc594c69bc46022f48c39a4c75ad1c3103fe055a65bf78af4b7cbc59a430066d |
| hear/sketch.py | 12254 | 3b35a11f2dd115cb6620925a06ebf77fffd6096db2cea15ead61c47f850128d0 |
| hear/wire.py | 17680 | afc80491583dc79985308ef77085f380ef0ba81e9b42a7bf39719a3d06710432 |
| testdata/sketch_golden.json | 109646 | 6c3cfbce52d54f01f9b6e21523d5f2dc426fa0b0562fbe193251a771d67516bd |

## Firmware and build metadata

| arduino_cli | esp32_core | default_board_class | fqbn |
| --- | --- | --- | --- |
| 1.5.1 | 3.3.11 | xiao-s3-pps | esp32:esp32:XIAO_ESP32S3:PSRAM=opi |

| board_class | release_stem | cpp_flags |
| --- | --- | --- |
| esp32s3-i2s-gps | hear_node-esp32s3-i2s-gps | -DHEAR_BOARD_ESP32S3_I2S_GPS |
| xiao-s3-pps | hear_node-xiao-s3-pps | (none) |

| sketch | board_class | guarded | artifact | build_flags |
| --- | --- | --- | --- | --- |
| hear_node | xiao-s3-pps | yes | fw-hear_node-xiao-s3-pps | -DHEAR_ALLOW_NO_WIFI |
| hear_node | esp32s3-i2s-gps | yes | fw-hear_node-esp32s3-i2s-gps | -DHEAR_ALLOW_NO_WIFI -DHEAR_BOARD_ESP32S3_I2S_GPS |
| hear_node | esp32s3-i2s-gps | yes | fw-hear_node-esp32s3-i2s-gps-qspi | -DHEAR_ALLOW_NO_WIFI -DHEAR_BOARD_ESP32S3_I2S_GPS |
| puc_node | (none) | yes | fw-puc_node | -DHEAR_ALLOW_NO_WIFI |
| hear_poc | (none) | no | fw-hear_poc | -DHEAR_ALLOW_NO_WIFI |
| path_test | (none) | no | fw-path_test | -DHEAR_ALLOW_NO_WIFI |
| sense_bringup | (none) | no | fw-sense_bringup | -DHEAR_ALLOW_NO_WIFI |

## Schemas

| contract | current |
| --- | --- |
| pool schema version | 1 |
| receiver schema version | 1 |
| telemetry schema version | 1 |
| dets.csv latest | G7 |
| scene.csv latest | S2 |

### Telemetry routes

| http_path | telemetry_path | fields | event_types | route_hash |
| --- | --- | --- | --- | --- |
| /api/hear/heartbeat | hear/heartbeat | 15 | (n/a) | 2d79cc68fbc256079267fbc9cba1a859826417ba4691a6c106c848e9570159ff |
| /api/hear/event | hear/event | 16 | clip_written, detection_batch_ready | 9baf63637473382626f04e29f01f6741e90657ed2e2ad1c84394dd7131b2e094 |

### Literal schema identifiers found in repo

| schema | source_paths |
| --- | --- |
| acoustic_latency_calibration.v1 | tools/hear_latency_cal.py |
| hear.calibrated_node_biases.v1 | tools/calibrate_claps.py |
| hear.clip_tag.v2 | tools/hear_tag.py |
| hear.experimental_clap_geometry_selfcal.v1 | tools/estimate_geometry_from_claps.py |
| hear.ingest.v1 | hear/ingest/envelope.py |
| hear.node_positions.v1 | tools/hear_drain.py |
| hear.sketch.golden.v4 | testdata/sketch_golden.json<br>tools/gen_golden.py |
| hear.sketch_score.v1 | tools/hear_score.py |
| hear.tdoa_arrival.v1 | tools/hear_tdoa.py |
| hear.tdoa_attempt.v1 | tools/hear_tdoa.py |
| hear.tdoa_model_card.v1 | tools/hear_tdoa.py |
| hear.window.golden.v2 | testdata/window_golden.json<br>tools/gen_golden.py |

## MQTT topics

| topic_pattern | role | source_paths |
| --- | --- | --- |
| dama/+/acoustic_sketch | captured sketch ingest and replay lane | hear/corpus.py<br>hear/pool.py<br>tools/hear_score.py |
| dama/+/telemetry | bridge subscription for hear_node telemetry relayed through MQTT | tools/hear_mqtt_bridge.py<br>deploy/k8s/hear-mqtt-bridge.yaml |
| dama/hear/spatial_events | optional GeoJSON publish from the spatial pipeline | hear/spatial.py<br>tools/hear_spatial.py |

## Redis keys

| pattern | kind | writer | ttl/maxlen |
| --- | --- | --- | --- |
| dama:hear:{device_id} | string-with-ttl | HeartbeatReceiverStore.write_heartbeat | 30 |
| dama:hear:devices | set | HeartbeatReceiverStore.write_heartbeat/write_event | (n/a) |
| dama:hear:latest | string | HeartbeatReceiverStore.write_heartbeat | (n/a) |
| dama:hear:event:{device_id} | string | HeartbeatReceiverStore.write_event | (n/a) |
| dama:hear:events | stream | HeartbeatReceiverStore.write_event | 1024 |

## Kubernetes and PVC layout

Manifest hashes:

| path | size_bytes | sha256 |
| --- | --- | --- |
| deploy/k8s/hear-annotate.yaml | 27683 | 626e06637419460bc35de39328e1a75e072946d0c192a12ce9f1c8ea079a4061 |
| deploy/k8s/hear-birdnet.yaml | 7474 | 9a4265e78c5c03e10a563b1a178582492e3f0aeef0c47f630f56fe23c06c6aa5 |
| deploy/k8s/hear-drain.yaml | 13594 | 38e02f09e8b124e5d321a7c51a97e93b898b03e27de6d19a0d38fb9b00cedf4e |
| deploy/k8s/hear-embed.yaml | 9190 | 1f3e86e8d5d1f57f4bc5d8d4ca6adb958671f5a65c7db7b21e47225916d465ad |
| deploy/k8s/hear-heartbeat.yaml | 3663 | 2fe073497c87d1b0de637a5adba377f84cf1b18cf6a3cb2d0c47bec4016dd3e8 |
| deploy/k8s/hear-mqtt-bridge.yaml | 4781 | 191e575074cd1960d4ecd0fd502a4ef999377061242b62bc3ea7ba006b1d8caf |
| deploy/k8s/hear-score.yaml | 11126 | 03c0713d22b23d3f06177c79ca6d612925768c98d566f012fc01d209de5dcf0d |
| deploy/k8s/hear-tag.yaml | 15625 | 367ed67528bb15a5dd652bf924db0af409a8f65c1b00128c0a3b14e990be1890 |
| deploy/k8s/hear-tdoa.yaml | 17920 | 8ff0135e4b56f76d7b0d69ccdc4f9129efd3050047698cc9161b002cfaae2cc8 |
| deploy/k8s/README.md | 15104 | a7f7487543e350ef79649f441e0b3c7439cadcdf5d073b9072babbfd99a5bc18 |

### PVCs

| claim | storage | access_modes | consumers | paths |
| --- | --- | --- | --- | --- |
| hear-heartbeat-state | 5Gi | ReadWriteOnce | Deployment hear-heartbeat receiver:/state | (none) |
| hear-mqtt-bridge-state | 5Gi | ReadWriteOnce | Deployment hear-mqtt-bridge bridge:/state | (none) |
| hear-pool | 5Gi | ReadWriteOnce | CronJob hear-drain drain:/pool<br>CronJob hear-drain-check check:/pool<br>CronJob hear-embed embed:/pool<br>CronJob hear-embed-check check:/pool<br>CronJob hear-score score:/pool<br>CronJob hear-score-check check:/pool<br>CronJob hear-tag tag:/pool<br>CronJob hear-tag-check check:/pool<br>CronJob hear-tdoa tdoa:/pool<br>CronJob hear-tdoa-check check:/pool | /pool<br>/pool/corpus<br>/pool/corpus/clips/tags.jsonl<br>/pool/corpus/scores<br>/pool/corpus/tdoa<br>/pool/corpus/tdoa/arrivals<br>/pool/corpus/tdoa/model_card.json<br>/pool/corpus/tdoa/runs<br>/pool/models/mn10_as<br>/pool/models/perch_v2<br>/pool/pylib<br>/pool/pylib-perch<br>/pool/pylib-tag<br>/pool/sketch_corpus |

### Objects

| manifest | kind | name | schedule | hostNetwork | containers |
| --- | --- | --- | --- | --- | --- |
| deploy/k8s/hear-annotate.yaml | ConfigMap | hear-annotate-code | (n/a) | no | (n/a) |
| deploy/k8s/hear-annotate.yaml | Deployment | hear-annotate | (n/a) | no | web |
| deploy/k8s/hear-annotate.yaml | Service | hear-annotate | (n/a) | no | (n/a) |
| deploy/k8s/hear-birdnet.yaml | CronJob | hear-birdnet | 19,49 * * * * | no | birdnet |
| deploy/k8s/hear-birdnet.yaml | CronJob | hear-birdnet-check | 58 * * * * | no | check |
| deploy/k8s/hear-drain.yaml | PersistentVolumeClaim | hear-pool | (n/a) | no | (n/a) |
| deploy/k8s/hear-drain.yaml | CronJob | hear-drain | */15 * * * * | no | drain |
| deploy/k8s/hear-drain.yaml | CronJob | hear-drain-check | 17 * * * * | no | check |
| deploy/k8s/hear-embed.yaml | CronJob | hear-embed | 4,34 * * * * | no | embed |
| deploy/k8s/hear-embed.yaml | CronJob | hear-embed-check | 38 * * * * | no | check |
| deploy/k8s/hear-heartbeat.yaml | PersistentVolumeClaim | hear-heartbeat-state | (n/a) | no | (n/a) |
| deploy/k8s/hear-heartbeat.yaml | Deployment | hear-heartbeat | (n/a) | yes | receiver |
| deploy/k8s/hear-heartbeat.yaml | Service | hear-heartbeat | (n/a) | no | (n/a) |
| deploy/k8s/hear-mqtt-bridge.yaml | PersistentVolumeClaim | hear-mqtt-bridge-state | (n/a) | no | (n/a) |
| deploy/k8s/hear-mqtt-bridge.yaml | Deployment | hear-mqtt-bridge | (n/a) | yes | bridge |
| deploy/k8s/hear-score.yaml | CronJob | hear-score | 7,22,37,52 * * * * | no | score |
| deploy/k8s/hear-score.yaml | CronJob | hear-score-check | 27 * * * * | no | check |
| deploy/k8s/hear-tag.yaml | CronJob | hear-tag | 12,42 * * * * | no | tag |
| deploy/k8s/hear-tag.yaml | CronJob | hear-tag-check | 52 * * * * | no | check |
| deploy/k8s/hear-tdoa.yaml | CronJob | hear-tdoa | 11,26,41,56 * * * * | no | tdoa |
| deploy/k8s/hear-tdoa.yaml | CronJob | hear-tdoa-check | 47 * * * * | no | check |

## Representative corpus fixture metadata

Representative fixture inventory only: repo paths, top-level schema ids, counts, sizes, and sha256 digests. No live corpus pulls and no payload bodies are copied.

| path | schema | summary | size_bytes | sha256 |
| --- | --- | --- | --- | --- |
| docs/data/clap-calibration-2026-09-14/clean_clap_arrivals.json | (none) | {"top_level_keys": ["observations"], "type": "object"} | 3469 | 4fe8c28df91c876b3020c131b135a7885b6c9f5876bb76da84cacef799ebad0e |
| docs/data/clap-calibration-2026-09-14/colocated_survey.json | (none) | {"nodes_count": 6, "top_level_keys": ["nodes"], "type": "object"} | 369 | e5d047bb0cc3a76de4ab7353b195512b2b928e44c635b3c07fe56a1c9ce4e99d |
| testdata/hugbot_latency_trials.json | (none) | {"length": 450, "type": "array"} | 143346 | 72f50300ca13d7d4bbf01ff0959dc4512d1d4f96b1a81eb3872e84992ff2b254 |
| testdata/sketch_golden.json | hear.sketch.golden.v4 | {"cases_count": 9, "schema": "hear.sketch.golden.v4", "top_level_keys": ["cases", "f_hi", "f_lo", "frames", "fs_codes", "fs_mask", "fs_shift", "hop_s", "layout_bit", "layout_equivalent_above_hz", "mel_bands", "nfft", "note", "schema", "wire_size"], "type": "object"} | 109646 | 6c3cfbce52d54f01f9b6e21523d5f2dc426fa0b0562fbe193251a771d67516bd |
| testdata/window_golden.json | hear.window.golden.v2 | {"cases_count": 4, "schema": "hear.window.golden.v2", "top_level_keys": ["cases", "env_ms", "guard_s", "note", "onset_frac", "retrigger_s", "schema"], "type": "object"} | 52751 | cadf188a841010ea11c969a053b1f57970832f9cb0a8164d3da9e65d32ea4d41 |
| tests/fixtures/status_mach.json | (none) | {"top_level_keys": ["acq", "audio", "class", "clips", "env", "esp_clock", "gate", "gps", "heap", "i2c", "i2s", "node", "pos", "pps", "psram", "raw", "scene", "sd", "sd_free_mb", "sd_total_mb", "time", "uptime_s", "write_fail"], "type": "object"} | 2111 | 2d822b7f2f640ce6e8f2f987727dbda0458a7959c681862ecce15c61f1b1e881 |
| tests/fixtures/status_nyquist.json | (none) | {"top_level_keys": ["acq", "audio", "class", "clips", "env", "esp_clock", "gate", "gps", "heap", "i2c", "i2s", "node", "pos", "pps", "psram", "raw", "scene", "sd", "sd_free_mb", "sd_total_mb", "time", "uptime_s", "write_fail"], "type": "object"} | 2113 | fc6caf8751a1438c474a205e816f6b5772fb294e42a17c0c976d8b5e15d3dc57 |
| tests/fixtures/tdoa-solved-events-2026-09-11.json | (none) | {"events_count": 2, "top_level_keys": ["events", "how_to_read", "provenance", "what"], "type": "object"} | 8191 | 027f8fd72650a8371238fee45cee91aac08f8d16985284bb207b7c20d7432a86 |

