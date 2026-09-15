# Fleet hardware remediation plan — 2026-09-15

Executable, risk-ranked plan for every open fleet hardware item. Produced from a **fresh clone of
`origin/main` (`07077ea`)** and a **read-only** live inspection of the fleet at
2026-09-15T14:35Z. Nothing was flashed, written to a node, or changed in the cluster to produce
this document.

Roster (from `deploy/k8s/hear-drain.yaml`): `nyquist=172.16.100.105`, `mach=172.16.100.116`,
`rankine=172.16.100.50`, `gold=172.16.100.82`, `kasami=172.16.100.90`, `ageev=172.16.100.83`.

---

## 0. Evidence base (read-only, 2026-09-15T14:35Z)

Collected with `curl -s -m 12 http://<ip>/status`, `/audio`, `/detections` and
`kubectl -n dama logs` only.

| node | fw | class | selftest mic | live mic evidence | psram | clock |
|---|---|---|---|---|---|---|
| ageev | v0.1.4-123-g5c8a818 | esp32s3-i2s-gps | **capture-failure / all_zero_samples** (768 samples, span 0) | `/audio` 1 s window: 48 000 samples, min −66 max 86, 151 unique, σ=22.2; `i2s.samples` 118 376 960, `clean_s` 6693; `gate.e_max_win` 13 052; 16 detections drained | 364 984 free, 80 s raw ring full | HOLDOVER, anchor 0.96 s |
| gold | v0.1.4-123-g5c8a818 | esp32s3-i2s-gps | **capture-failure / all_zero_samples** | `/detections` returns 3 real rows with 172-byte spectral frames and triggers −9110 / −2108 / −1083; `i2s.samples` 117 087 744, `clean_s` 6619; `gate.e_max_win` 12 596 | **`psram` 0, `psram_min` 0, `raw.span_s` 0, `heap_min` 108 B, `loop_max_ms` 5117** | HOLDOVER, **anchor 3715 s**, σ 74.3 ms (≈575× `ARRIVAL_T_SIGMA_MAX_S`) |
| kasami | v0.1.4-123-g5c8a818 | esp32s3-i2s-gps | normal, but **marginal** (span 368, lo 0, mean_abs 10, unique 17, same_adj 97 %) | `/audio` 1 s: 48 000 samples, min −37 max 46, 77 unique, σ=9.5; `gate.e_max_win` 9863 | 362 612 free, 80 s ring (**octal image works**) | HOLDOVER, anchor 1.1 s, `heap_min` 2008 B |
| mach | **v0.1.4-5-g2355270 (118 commits stale)** | xiao-s3-pps | `"ok"` — old firmware, **no `mic_state`/`mic_stats` at all** | `gate.e_max_win` **54.7**, `headroom` 0.27, `dc` **1094.6**, `i2s.ppm` −964.3 | 345 152 free | `time.state` absent (pre-clock-state build) |
| nyquist | v0.1.4-123-g5c8a818 | xiao-s3-pps | normal (span 21 507) | healthy reference | ok | LOCKED |
| rankine | v0.1.4-123-g5c8a818 | xiao-s3-pps | (not probed this pass) | drain reports timeouts on two files this run | ok | LOCKED |

Cluster (read-only): `hear-drain` CronJob **succeeds**; `hear-drain-check` **exits 1 on every run
for the last ~8 h** (`hear-drain-check-29824697-dvzr8`, exitCode 1). The only failing assertion in
its output is:

```
⚠️FLEET IS SPLIT across 2 builds: v0.1.4-123-g5c8a818, v0.1.4-5-g2355270
```

i.e. **the paging alert that is firing right now is caused by mach's stale firmware, nothing else.**

### Conclusions the evidence forces

1. **Ageev's microphone is healthy.** `selftest_mic_probe()`
   (`firmware/hear_node/hear_node.ino:3923`) reads exactly one `ABLOCK` = 768 samples = 16 ms
   immediately after `i2s.begin()` (`hear_node.ino:4893`/`:4910`) with **no settle delay**. A cold
   ICS-43434 returns zeros over that window, `mic_diag_classify()` returns
   `capture-failure/all_zero_samples`, and `selftest_mic` is **latched for the whole boot** — it is
   never re-probed. **No ageev hardware work is justified. Do not replace ageev's microphone.**
2. **Gold's microphone is not dead either** — it is producing real, varying audio *right now* on
   the shipping pin map (BCLK 41 / WS 1 / DIN 42, `firmware/boards/esp32s3_i2s_gps.h`). Its three
   detections carry non-trivial trigger amplitudes and full spectral frames. The prior "dead mic"
   diagnostic used a transposed WS/DIN map and is void.
3. **Gold's real fault is PSRAM.** `psram_min 0` + `raw.span_s 0` + `heap_min 108 B` is exactly the
   octal-image-on-quad-board signature described in `firmware/hear_node/board_profiles.py` and
   `docs/REDESIGN-LESSONS.md` item 8. This is an active reliability hazard (108 bytes of headroom)
   **and** the reason `/audio` cannot be used to close out gold's microphone.
4. **Kasami's silicon runs the octal class default correctly** (7.68 MB ring allocated, 362 kB
   free). Its microphone is alive but ~7 dB quieter than ageev's.
5. **Mach is reachable over HTTP and drains normally.** The stated assumption that mach needs
   *physical* recovery is not supported by today's evidence; see R3.

---

## 1. Risk ranking

Ranked by `active harm now × blast radius × irreversibility`.

| # | Item | Severity | Owner | Blocks |
|---|---|---|---|---|
| **R1** | Gold quad-PSRAM flash | **Critical** — 108 B heap headroom, node can fail to allocate at any moment; no raw ring, no `/audio`, no TDoA-grade evidence | **Physical (USB)** | R5 |
| **R2a** | Stop the false `capture-failure` page in `check_fleet_health.py` (no flash) | **High** — a false hardware verdict already nearly caused an unnecessary mic replacement | Software | — |
| **R2b** | Narrow `selftest_mic_probe()` settle-delay firmware fix | **High** — every cold boot fleet-wide mislabels healthy mics | Software + one physical power-cycle for the decisive test | R5 acceptance |
| **R3** | Mach data pull, then reflash | **High** — clip budget exhausted (`budget_left_clips` 0, 49 evicted, 96 `skip_ring`); stale build is the sole cause of the currently failing `hear-drain-check` | Software first, physical only as fallback | drain-check green |
| **R4** | Gold GPS sky/antenna remediation | **Medium** — anchor 3715 s stale, σ 74 ms, `fix` 6 (dead-reckoned), `hacc` 250 m; blocks clap calibration and TDoA admission | **Physical** | TDoA/calibration |
| **R5** | Corrected gold microphone validation | **Medium** — currently an unproven negative in the record | Software (after R1) | closing the "gold mic" question |
| **R6** | Kasami marginal mic + PSRAM verification | **Low** — node is functional and contributing | Software + optional physical scan | — |

### Dependency graph

```
R2a ─────────────────────────────────────────► (immediate, independent)

R1 (gold USB quad flash) ──► R5 (gold mic validation via /audio)
        ▲                          ▲
        └── R4 (gold antenna) ─────┘   (same site visit; R4 independent of R1 but shares the trip)

R2b (settle fix) ──► R2b cold-boot proof on ageev ──► fleet rollout ──► R5 acceptance uses it
                                                                  └──► R6 kasami re-probe

R3a (mach data pull) ──► R3b (mach reflash) ──► drain-check green + mach gains mic diagnostics
```

Ordering rule: **R2a today (no flash). R1 + R4 on the next site visit. R3 independently. R2b
staged after R2a. R5 and R6 last, because they consume R1 and R2b.**

---

## R1 — Gold quad-PSRAM flash (Critical, physical)

**Why USB and not OTA.** PSRAM bus mode is compile-time (`CONFIG_SPIRAM_MODE_OCT`) and the quad
variant is built from a *different board definition with a different partition scheme*
(`esp32:esp32:esp32s3:PSRAM=enabled,FlashSize=8M,PartitionScheme=default_8MB,...` vs
`esp32:esp32:XIAO_ESP32S3:PSRAM=opi`, `board_profiles.py:PSRAM_MODES`). An OTA writes the app slot
only; it cannot replace the bootloader or the partition table. Gold therefore needs bootloader +
partitions + app together, which only a USB upload delivers.

**Preferred command** (build mode, from this clone, with gold physically on USB):

```bash
arduino-cli version                      # toolchain must be present
python3 firmware/hear_node/flash.py gold /dev/ttyACM0 --class esp32s3-i2s-gps
```

`flash.py` resolves `board_profiles.fqbn("esp32s3-i2s-gps", "gold")` → the **quad** FQBN via
`NODE_PSRAM_MODES["gold"]`, writes bootloader, partitions and app from `--input-dir`, and leaves
NVS untouched.

**Release-image alternative** (only if the build toolchain is unavailable): flash
`hear_node-esp32s3-i2s-gps-qspi-v0.1.5-merged.bin` (published on `v0.1.5`, verified present) with
`esptool` at offset `0x0`, then re-verify identity. Verify the checksum against the release
`SHA256SUMS` first.

**Acceptance checks** (all must pass; read-only):

```bash
curl -s http://172.16.100.82/status | python3 -m json.tool | \
  grep -E '"node"|"fw"|"psram"|"psram_min"|"heap_min"|"loop_max_ms"|"span_s"'
curl -s http://172.16.100.82/audio          # must report ring:true, addressable:true
```

| check | pass condition | today's value |
|---|---|---|
| `node` | `"gold"` | gold |
| `fw` | the version just built/installed, `prov.src` unchanged | v0.1.4-123 |
| `psram` / `sys.psram_min` | **> 0** | 0 / 0 |
| `raw.span_s` / `raw.want_s` | **> 0 and stable.** 2 MB quad is a much shorter ring than ageev's 80 s: an image carrying `PRAW_TIERS_S` continues below 30 s to 20/15/10 (`firmware/hear_node/README.md`), so the expected landing is **10 s or 15 s** — `raw.want_s` names the tier exactly. Take whatever the boot log reports as the baseline rather than assuming a number; `docs/REDESIGN-LESSONS.md` item 12 says "~15 s on Gold/Kasami", but kasami measurably holds 80 s today, so that figure is not a specification. An image WITHOUT the small tiers gives 0 here on this part no matter how healthy it is. | 0 |
| `sys.psram_total` | **2 097 152** (2 MiB) once the quad image answers; it is the field that makes a short ring readable as silicon rather than as a fault | absent (pre-change image) |
| `sys.heap_min` | **> 20 000 B** after ≥ 1 h uptime | 108 |
| `sys.loop_max_ms` | **< 1000** after ≥ 1 h uptime | 5117 |
| boot log | `psram <n> kB, quad bus (as built)`, and **no** `psram FAULT` | FAULT expected today |

**Rollback.** Keep gold's current image identifiable before starting (record `fw` and
`prov`). If the quad image misbehaves, re-flash the octal class image over USB the same way
(`--class esp32s3-i2s-gps` after temporarily reverting gold in `NODE_PSRAM_MODES`) — a rollback is
another USB visit, so **do not start R1 without physical access for a second attempt**.
The on-node boot guard cannot rescue a bad *bootloader*, only a bad app.

**Do not:**
- Do not `esptool.py erase_flash` on gold. It destroys the NVS enrollment record
  (`prov.nvs: true`) — identity and Wi-Fi credentials — and gold then cannot be reached to be
  fixed.
- Do not push a quad app over `/update` (OTA). Partition table and bootloader would stay octal.
- Do not use `flash.py --release` over serial; it refuses by design.
- Do not touch `kasami` in the same operation: its bus mode is deliberately **absent** from
  `NODE_PSRAM_MODES` (see R6).

---

## R2a — Stop the false page without flashing anything (High, software, do first)

`tools/check_fleet_health.py:_mic_issue()` (line 581) pages on
`mic_state in {capture-failure, stuck, floating, saturated}` with **no corroboration** from the
node's live counters. Ageev and gold both page today while demonstrably capturing audio.

**Change (one seam, no firmware):**
1. In `_status_parse()` (≈ line 495-535) also parse `audio.detections`, `gate.e_max_win`,
   `gate.thr`, `i2s.samples` and `i2s.clean_s`.
2. In `_mic_issue()`, when `mic_state == "capture-failure"` **and** the live counters contradict it
   (`i2s.clean_s` > 60 **and** `gate.e_max_win` > `gate.thr` — i.e. the acquisition path is
   running and the envelope is moving), downgrade the page to an informational
   `mic=capture-failure (boot probe only; live capture contradicts it)` finding.
   Keep `stuck`/`floating`/`saturated` paging unchanged — those are *sustained* classifications,
   not a one-shot boot probe.

**Tests to add** in `tests/test_check_fleet_health.py`:
- a status fixture shaped exactly like today's ageev `/status` ⇒ **no** page;
- the same fixture with `i2s.clean_s = 0` and `gate.e_max_win < thr` ⇒ **still pages**;
- `mic_state = "stuck"` with healthy counters ⇒ **still pages** (no regression of real faults).

**Rollback:** revert the commit; it is a pure reporting change with no node interaction.

**Do not:** do not suppress the field or delete `capture-failure` from the paging set — the state
must remain visible and must still page when nothing corroborates the mic.

---

## R2b — Narrow selftest settle-delay firmware fix (High, software)

**Exact defect.** `hear_node.ino:3923-3932`:

```c
static void selftest_mic_probe() {
  int16_t probe[ABLOCK];                              // ABLOCK = BLOCK*DECIM = 768 @ 48 kHz = 16 ms
  size_t got = i2s.readBytes((char *)probe, sizeof probe);
  ...
  selftest_mic_set_diag(mic_diag_classify(probe, (size_t)n));
}
```

called immediately after `i2s.begin()` at `:4893` (PDM) and `:4910` (I2S). The result is latched in
`selftest_mic` for the whole boot and is never re-probed.

**Fix as shipped (bounded settle window, classification thresholds unchanged):**
1. `mic_probe_settle()` in `firmware/hear_node/mic_diagnostics.h` reads one `ABLOCK` block,
   classifies it, and **accepts the first healthy verdict** (`quiet` or `normal`). Anything else --
   `capture-failure`, `stuck`, `floating`, `saturated` -- is a shape a not-yet-awake part produces,
   so it is retried after `MIC_PROBE_RETRY_GAP_MS` (20 ms, ~one DMA block) until the budget or
   `MIC_PROBE_MAX_ATTEMPTS` (16) runs out. The **last** classification is what is latched, so an
   absent, stuck or floating microphone is reported exactly as before.
2. The budget follows the reset reason, not a constant: `MIC_PROBE_SETTLE_COLD_MS` (750 ms) on a
   power-on/brownout/external reset where the part comes up unpowered, `MIC_PROBE_SETTLE_WARM_MS`
   (250 ms) after a software/OTA restart where the microphone kept its supply and answers the first
   read. A healthy node therefore pays **nothing**: one read, no wait, on either path.
3. The wait services the boot watchdog (`boot_wait_ms()`), and the worst case (750 ms) sits two
   orders of magnitude inside the existing `boot_wdt_arm(15000)` window. Capture timing is
   untouched: the probe only drains stale DMA before `loop()` starts counting samples.
4. The evidence is published: `selftest.mic_stats.attempts` and `selftest.mic_stats.settle_ms` in
   `/status`, plus a `selftest mic_state=... attempts=... settle_ms=...` boot log line, so a
   settled probe is distinguishable from an unsettled one. `silent` after one read and `silent`
   after the whole window are different claims and now read differently.

**Tests** (`tests/test_firmware_mic_settle.py`, pure C through the existing `ctypes` harness --
no hardware, a scripted fake microphone and a fake clock):
- cold all-zero block then real audio settles `normal` (ageev's boot);
- cold repeated-sample blocks then real audio settles `normal` (kasami's boot);
- a short/empty first read is retried rather than latched;
- a microphone that never wakes is still `capture-failure/all_zero_samples`, one that never returns
  samples is still `capture-failure/no_samples`, a stuck one is still `stuck`, a floating pin is
  still `floating`;
- the window is bounded in wall time and attempts, a zero budget is the old single-read behaviour,
  and a warm budget is shorter than a cold one;
- the firmware still wires the probe through the settle window and reports the new fields.
`tests/test_firmware_mic_diagnostics.py` keeps the normal/quiet/marginal classification assertions
unchanged, and `tests/test_nvs_enrollment.py:92`'s `boot_wdt_disarm()` ordering still holds.

Run: `python3 -m pytest tests/test_firmware_mic_settle.py tests/test_firmware_mic_diagnostics.py tests/test_nvs_enrollment.py -q`

**Staged rollout, in this order:**

| stage | node | why this order | gate before proceeding |
|---|---|---|---|
| 1 | `kasami` | healthy, contributes least, and its *marginal* boot stats (span 368, lo 0) are the second-best evidence of an unsettled probe | boots, `/status` reachable, `mic_state` ∈ {normal, quiet}, `raw.span_s` unchanged |
| 2 | `ageev` | **the decisive node** — it is the one that latches the false verdict | 3 consecutive **cold power-cycles** each report `mic_state` ∈ {normal, quiet} with `span > 0` |
| 3 | `nyquist`, `rankine` | reference nodes; prove no regression on a known-good mic | `mic_state` still `normal`, `span` comparable to today's 21 507 |
| 4 | `mach` | folded into R3's reflash | see R3 |
| 5 | `gold` | **only after R1** | see R5 |

**⚠️ The cold-boot proof must be a real power cycle.** An OTA reboot does not remove power from the
ICS-43434, so the cold-zero condition may not reproduce and an OTA-only "pass" proves nothing.
Stage 2's gate therefore needs a physical power-cycle of ageev (3×).

**Rollback gates:** any of — node fails to answer `/status` within 120 s, `fw` reverts (boot guard
fired), `raw.span_s` drops, `sys.loop_max_ms` rises above 1000, or `mic_state` on a known-good node
degrades ⇒ stop the rollout and restore with
`python3 firmware/hear_node/flash.py <node> <ip> --release v0.1.5`.

**Do not:**
- Do not loosen `mic_diag_classify()` thresholds. The classifier is correct; the *sampling moment*
  is wrong. Changing thresholds would blind the fleet to genuinely dead mics.
- Do not make the probe unbounded or move it into `loop()`; the boot WDT window is the safety net.
- Do not roll all six nodes in one pass, and do not roll gold before R1.

---

## R3 — Mach: data pull, then reflash (High)

**Correction to the stated assumption.** Mach answered `/status` in 79 ms today, drains normally
(`+37 clips fetched over 7 runs`, scene rows written 7158) and is on the network. **There is no
current evidence that mach needs physical recovery.** Treat mach as an OTA target and escalate to
USB only on a concrete failure.

**R3a — data pull first (do before any reboot).** A reboot loses the 80 s PSRAM raw ring and the
in-RAM detection ring; the SD card survives. Mach is also *currently losing clips*:
`clips.budget_left_clips 0`, `evicted 49`, `skip_ring 96`.

```bash
curl -s "http://172.16.100.116/detections" -o mach_detections_preflash.json
curl -s "http://172.16.100.116/audio"      # read the window, then fetch inside it:
curl -s "http://172.16.100.116/audio?from=<from_utc_us+3000000>&dur=10" -o mach_raw_preflash.wav
kubectl -n dama create job --from=cronjob/hear-drain hear-drain-preflash-mach   # ⚠️ see below
```

⚠️ The `kubectl create job` line **alters the cluster** and is therefore **out of scope for this
plan** — it is listed only so the operator who does have that authority knows the pre-flash drain
is the intended way to flush mach's SD tail. If it is not run, simply wait for the next scheduled
`hear-drain` run to report `mach ok` before flashing.

**R3b — reflash (OTA first):**

```bash
python3 firmware/hear_node/flash.py mach 172.16.100.116 --release v0.1.5
```

`flash.py` refuses a wrong identity, and — because release mode requires `prov.src == "nvs"` —
refuses a node that is not enrolled. Escalate to USB (`flash.py mach /dev/ttyACM0`) **only** if the
OTA is rejected, or if `flash.py` reports the boot guard reverted the image (identity matches, `fw`
does not).

**Acceptance checks:**
- `fw` == `v0.1.5` and `prov.src` == `"nvs"` (flash.py asserts both);
- `hear-drain-check` exits **0** on its next run — the fleet-split warning is the only assertion
  failing today;
- mach's `/status` now carries `selftest.mic_state`/`mic_stats` and `time.state`, which the
  v0.1.4-5 build does not emit at all;
- **new information this buys:** mach's `gate.e_max_win` is 54.7 with `dc` 1094.6 and `i2s.ppm`
  −964.3 — a large DC offset on a tiny envelope. This is the first node whose microphone looks
  genuinely suspect, and the current build has no diagnostics to say so. After the reflash, read
  `selftest.mic_stats` and open a **separate** investigation if `mic_state` is not `normal`/`quiet`.
  Do not pre-judge it as a hardware fault — that is the mistake this whole plan exists to undo.
- mach's SD retention: commit `07077ea` makes retention capacity relative; confirm
  `clips.budget_left_clips > 0` after the new image settles, or file clip-budget tuning separately.

**Rollback:** `python3 firmware/hear_node/flash.py mach 172.16.100.116 --release v0.1.4` (the
version family it is closest to), or let the on-node boot guard revert automatically.

**Do not:**
- Do not reflash mach before the pre-flash drain/pull; clips are already being evicted.
- Do not drive out to mach before trying OTA.
- Do not silence the `hear-drain-check` fleet-split assertion. It is doing its job.

---

## R4 — Gold GPS sky/antenna remediation (Medium, physical)

**Evidence.** Gold today: `gps.fix 6` (NMEA GGA quality 6 = *estimated / dead-reckoned*, not a live
satellite solution), `gps.sats 2`, `pos.hacc_m 249.97`, `pos.n 708` (vs ageev's 6480),
`time.anchor_age_us 3 714 969 490` (**62 minutes stale**), `sync_sigma_ns 74 324 390`
(**≈575× `hear/nodeclass.py:ARRIVAL_T_SIGMA_MAX_S` = 129.4 µs**, the per-node arrival budget for
this array), `pmtk_ack 5 / nak 0` (the module itself is alive and configured). `pps.spread_us` is
only 30 — the PPS *edge* is clean; the *GNSS solution behind it* is not. This is the failure mode
analysed in `docs/findings-clap-calibration-2026-09-14.md`; gold and kasami have swapped which one
is dead-reckoning since that document was written.

⚠️ That findings document cites a constant `MAX_SYNC_SIGMA_NS` of 0.5 ms. **No such constant exists
in this tree.** The two real gates are `tools/hear_tdoa.py`'s aperture-relative
`--max-sync-sigma-ns` knob (default `DEFAULT_SYNC_SIGMA_FRAC` 0.10 × the tightest pair bound) and
the hardware class gate derived from `hear/nodeclass.py`. Use those, not the doc's number.
Separately, `esp32s3-i2s-gps` carries `path_bias_s=None` and is **refused for arrivals regardless
of clock quality** until its capture path is measured — fixing gold's sky does not by itself make
gold TDoA-eligible.

**Work (physical, one site visit — combine with R1):**
1. Move/raise gold's GPS antenna for clear sky. Gold's Wi-Fi tells the same siting story: it is the
   only node on a *different* BSSID (`c8:c6:fe:91:3a:05`) at `rssi −71` while ageev/kasami/mach sit
   on `fc:3d:73:3a:50:c6` at −52/−60/−57. Gold is physically the odd one out.
2. Check the antenna connector and cable for the PMTK module — `pmtk_ack 5/0` proves the *module*
   is fine, so an unseated or damaged antenna is the leading candidate.
3. Re-check after ≥ 30 min of settling.

**Acceptance checks** (read-only, ≥ 30 min after the change):

| field | pass condition | today |
|---|---|---|
| `gps.fix` | 1 or 2 (a **live** solution, not 6) | 6 |
| `gps.sats` | ≥ 6 sustained | 2 |
| `gps.pmtk_glitch` | growth rate falls (it is the anchor-rejection counter) | 26 and climbing |
| `time.anchor_age_us` | **< 5 000 000** (5 s) sustained | 3 714 969 490 |
| `time.sync_sigma_ns` | **< 129 400** (`ARRIVAL_T_SIGMA_MAX_S`, the per-node arrival budget) | 74 324 390 |
| `pos.hacc_m` | < 15 | 249.97 |

Only when `sync_sigma_ns` is under the 129.4 µs per-node budget may the co-located clap calibration be re-run, using the
corrected methodology in `docs/findings-clap-calibration-2026-09-14.md` (near-field `--survey`
geometry, reference-node-consensus clap association).

**Do not:**
- Do not write `config/calibrated_node_biases.json` from any data taken while
  `sync_sigma_ns` is over budget. The recovered "biases" (gold −442.7 ms etc.) are clock drift, not
  microphone latency, and the confidence gate exists to refuse exactly this.
- Do not re-survey gold's position in `survey.json` until the fix is strong; the current entries
  are correctly disclaimed as provisional.
- Do not attribute gold's stale anchor to firmware before the antenna is ruled out — ageev and
  kasami run the same code and hold anchors ~1 s old.

---

## R5 — Corrected gold microphone validation (Medium, software; depends on R1)

**The prior verdict is void.** It used a transposed WS/DIN map. The shipping map is
`firmware/boards/esp32s3_i2s_gps.h`: **`MIC_BCLK_PIN 41`, `MIC_WS_PIN 1`, `MIC_DIN_PIN 42`** —
a single compiled-in constant set, so a correct validation is simply "run the shipping firmware and
read the shipping endpoints". No re-wiring, no bespoke diagnostic sketch.

**Preliminary result already in hand (read-only, today):** gold's `/detections` returned three rows
with 172-byte spectral frames and triggers −9110 / −2108 / −1083, `i2s.samples` 117 087 744,
`clean_s` 6619, `gate.e_max_win` 12 596. **Gold's microphone is capturing.** What is still missing
is waveform-level confirmation, which needs the PSRAM ring (R1).

**Procedure, after R1 and after R2b reaches gold:**

```bash
curl -s http://172.16.100.82/status | python3 -m json.tool | grep -E 'mic_state|mic_stats|span_s'
curl -s http://172.16.100.82/audio                                   # ring:true, addressable:true
FROM=$(curl -s http://172.16.100.82/audio | python3 -c "import json,sys;print(json.load(sys.stdin)['from_utc_us']+3000000)")
curl -s "http://172.16.100.82/audio?from=$FROM&dur=1" -o gold_audio.wav
python3 - <<'PY'
import struct, statistics
b = open('gold_audio.wav','rb').read(); s = struct.unpack('<%dh' % ((len(b)-44)//2), b[44:len(b)-((len(b)-44)%2)])
print(len(s), min(s), max(s), len(set(s)), round(statistics.pstdev(s),1))
PY
```

**Acceptance (all four, and the comparison is against ageev/kasami measured the same way today):**

| check | pass condition | ageev today | kasami today |
|---|---|---|---|
| boot selftest | `mic_state` ∈ {normal, quiet}, `mic_stats.span` > 0 | span 0 (pre-fix) | span 368 |
| live waveform | ≥ 20 unique values and σ > 2 over a 1 s window | 151 uniq, σ 22.2 | 77 uniq, σ 9.5 |
| response | a clap raises `gate.e_max_win` well above `gate.thr` (800) | 13 052 | 9863 |
| pipeline | new rows appear on `/detections` and drain into `/pool/corpus` | 16 drained | 3 drained |

A **fail** is only declarable if the live waveform is degenerate (all-zero, constant, or two-level)
**while** `i2s.clean_s` is advancing. Only then does a pin/hardware hypothesis re-open — and the
first step would be re-reading the board header, not re-wiring.

**Do not:**
- Do not re-run any diagnostic that hard-codes its own pin map. Use the compiled board header.
- Do not conclude anything about gold's mic from a boot selftest taken **before** R2b lands.
- Do not order a replacement microphone for gold. Today's evidence contradicts a dead mic.

---

## R6 — Kasami marginal microphone and PSRAM verification (Low)

**PSRAM — already answered by live evidence.** Kasami reports `psram 364 984` free, `psram_min
362 612`, and a full 80 s / 7.68 MB raw ring while running the **octal class-default image**. A
quad-only board cannot do that. Kasami's silicon is therefore octal-compatible and **its current
build is correct** — no flash change is needed, and `NODE_PSRAM_MODES` needs no new entry (gold is
listed because it *differs* from its class default; kasami does not).

⚠️ This contradicts `docs/REDESIGN-LESSONS.md` item 12, which states the PSRAM ring is "~15 s on
Gold/Kasami, ~80 s on Ageev". Kasami measurably holds **80 s / 7.68 MB** today. Correct that line
when this plan's first item lands; until then, do not size anything from it.

**Do not** add kasami to `NODE_PSRAM_MODES` on the strength of this. The table's own docstring
requires a **bare-board flash/PSRAM scan** before a node is pinned, and inferring a hardware fact
from runtime behaviour is the Ageev-class mistake in a new costume. Record the runtime evidence;
leave the table alone. If someone ever has kasami on a bench: `esptool.py -p /dev/ttyACM0 flash_id`
plus the PSRAM line from the boot log closes it properly.

**Microphone — marginal, not faulty.** Boot probe: span 368, `lo` 0 (positive-only — a
partially-warmed ramp, itself corroborating R2b), `mean_abs` 10, `unique` 17, `same_adj_pct` 97.
Live: σ 9.5 vs ageev's 22.2 over the same 1 s method — roughly 7 dB quieter, with the same
`gate.thr` 800 and a working `e_max_win` of 9863.

**Steps (after R2b stage 1):**
1. Re-read the boot selftest after the settle fix. Expect `span` to rise materially; if it stays
   near 368 with a settled probe, the low level is real and not a sampling artefact.
2. Co-located A/B: place kasami next to ageev, clap, compare `gate.e_max_win` and the 1 s σ from
   `/audio` with the identical command. A sustained ≥ 6 dB deficit at 1 m is a real sensitivity
   difference (mounting, port obstruction, or the part itself) and justifies a **physical**
   inspection of the mic port/mounting — the cheapest first move is cleaning/clearing the acoustic
   port, not replacing the part.
3. Watch `sys.heap_min` (2008 B today — the lowest in the fleet after gold). Not a mic issue, but
   it is the next reliability item on this node; file it separately if it keeps falling.

**Do not** change `gate.thr`/`floor` on kasami to compensate for level. That hides the difference
in the one number the detection pipeline trusts, and mach's `floor_source: "file"` (200) already
shows how a per-node floor override outlives the reason for it.

---

## 2. Global "do not" list

1. **Do not replace ageev's microphone.** It is healthy; the verdict is a firmware sampling bug.
2. **Do not replace gold's microphone.** It is capturing right now with the shipping pin map.
3. **Do not `erase_flash` any node.** NVS holds identity and credentials.
4. **Do not OTA a PSRAM bus-mode change.** Bootloader and partition table do not travel over OTA.
5. **Do not accept an OTA-reboot as a cold-boot test** for the settle fix; it does not power-cycle
   the microphone.
6. **Do not write calibrated node biases** from data taken while `sync_sigma_ns` exceeds
   `ARRIVAL_T_SIGMA_MAX_S` (129.4 µs).
7. **Do not loosen `mic_diag_classify()` thresholds** or remove `capture-failure` from paging.
8. **Do not pin `kasami` in `NODE_PSRAM_MODES`** without a bare-board scan.
9. **Do not flash more than one node at a time**, and never gold and ageev in the same window —
   the fleet needs an unchanged control node to compare against.

---

## 3. Provenance

- Repository: fresh clone of `https://github.com/rjmendez/dama-hear.git` at `07077ea`
  (`fix(firmware): make SD retention capacity relative`).
- Live inspection: `GET /status`, `GET /audio`, `GET /detections` on 172.16.100.82/.83/.90/.105/.116
  and `kubectl -n dama get/logs` — **read-only, no writes, no flashes, no cluster changes**.
- Release assets confirmed present on `v0.1.5`, including the
  `hear_node-esp32s3-i2s-gps-qspi-*` quad variant (app, bootloader, partitions, merged, elf) plus
  `release-manifest.json` and `SHA256SUMS`.
