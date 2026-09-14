# Fleet Topology: Nine DAMA Acoustic Nodes + One Hugbot Corroborator

This document defines the coherent 10-device acoustic fleet topology for DAMA, avoiding double counting while establishing role distinctions, capture-path biases, timestamp provenance domains, timing uncertainties, and TDoA solver eligibility rules across all participant profiles.

## Summary Topology Table

| Node ID | Display Name | Hardware Profile | Deployment Role | Time Source / Domain | Uncertainty ($t_{\sigma}$) | Capture-Path Bias | TDoA Eligible? |
|---|---|---|---|---|---|---|---|
| `gold` | Gold | `esp32s3-i2s-gps` | Reference Node (Batch 1) | `utc_gps_pps` | $100\,\mu\text{s}$ | $\sim 20.8\,\mu\text{s}$ | Pending measurement gates |
| `kasami` | Kasami | `esp32s3-i2s-gps` | Reference Unit #2 | `utc_gps_pps` | $100\,\mu\text{s}$ | $\sim 20.8\,\mu\text{s}$ | Pending measurement gates |
| `ageev` | Ageev | `esp32s3-i2s-gps` | Reference Unit #3 | `utc_gps_pps` | $100\,\mu\text{s}$ | $\sim 20.8\,\mu\text{s}$ | Pending measurement gates |
| `nyquist` | Nyquist | `xiao-s3-pps` | Deployed Monitoring Node | `utc_gps_pps` | $100\,\mu\text{s}$ | $\sim 62.5\,\mu\text{s}$ | **Yes** (Active) |
| `mach` | Mach | `xiao-s3-pps` | Deployed Monitoring Node | `utc_gps_pps` | $100\,\mu\text{s}$ | $\sim 62.5\,\mu\text{s}$ | **Yes** (Active) |
| `rankine` | Rankine | `xiao-s3-pps` | Deployed Monitoring Node | `utc_gps_pps` | $100\,\mu\text{s}$ | $\sim 62.5\,\mu\text{s}$ | **Yes** (Active) |
| `phone-1` | DAMA Phone 1 | `gotchi-phone` | Mobile Acoustic Participant | `android_boottime_unanchored` | $25.1\,\text{ms}$ | $\sim 70.0\,\text{ms}$ | **No** (Refused for TDoA) |
| `phone-2` | DAMA Phone 2 | `gotchi-phone` | Mobile Acoustic Participant | `android_boottime_unanchored` | $25.1\,\text{ms}$ | $\sim 70.0\,\text{ms}$ | **No** (Refused for TDoA) |
| `phone-3` | DAMA Phone 3 | `gotchi-phone` | Mobile Acoustic Participant | `android_boottime_unanchored` | $25.1\,\text{ms}$ | $\sim 70.0\,\text{ms}$ | **No** (Refused for TDoA) |
| `hugbot` | Hugbot | `hugbot-corroborator` | Corroborator (10th device) | `local_host_rtc` | $> 50\,\text{ms}$ | Uncalibrated | **No** (Corroboration-only) |

## Fleet Breakdown & Accounting Rules

1. **DAMA Acoustic Node Count = 9**:
   - 3 ESP32-S3 I2S+GPS Reference Nodes (`gold`, `kasami`, `ageev`)
   - 3 Deployed ESP32-S3 PDM+GPS Nodes (`nyquist`, `mach`, `rankine`)
   - 3 DAMA Handset Participants (`phone-1`, `phone-2`, `phone-3`)
2. **Corroborator Device Count = 1**:
   - `hugbot` is the 10th device. Prior evidence requires that Hugbot remain **corroboration-only** and **outside the 9-node DAMA acoustic count**.
3. **Simultaneous TDoA Receivers $\le$ 6**:
   - Having 9 DAMA acoustic nodes does **NOT** imply 9 simultaneous TDoA-capable receivers.
   - The 3 DAMA phones are acoustic participants (emitting detections, classification, and bearings), but are **strictly refused** as TDoA arrival sources due to unanchored $70\,\text{ms}$ HAL/buffering bias and $25.1\,\text{ms}$ timing scatter.
   - Gold, Kasami, and Ageev are hardware-designed for TDoA (`esp32s3-i2s-gps`), but require physical measurement gates (`pps_observed`, `audio_path_calibrated`, `timing_path_measured`) before being admitted to live TDoA solves.

## Timestamp Provenance Domains

- **`utc_gps_pps`**: GPS 1PPS disciplined hardware timer. Used by `xiao-s3-pps` and `esp32s3-i2s-gps`. High precision ($t_{\sigma} \le 100\,\mu\text{s}$), admissible for TDoA solving.
- **`utc_ntp`**: Network Time Protocol sync. Used by `puc-ntp`. Moderate precision ($t_{\sigma} \sim 3\,\text{ms}$), refused for TDoA arrival solving.
- **`android_boottime_unanchored`**: Android OS monotonic time without `AudioRecord.getTimestamp()` frame alignment. Used by `gotchi-phone`. High latency scatter ($t_{\sigma} \sim 25.1\,\text{ms}$) and large per-device HAL bias ($\sim 70\,\text{ms}$), refused for TDoA.
- **`local_host_rtc`**: Uncalibrated host clock / RTC. Used by `hugbot-corroborator`. Refused for TDoA.

## Event Contract (`hear/event_contract.py`)

Every acoustic event emitted across the fleet follows the contract:
- `event_id`: Unique identifier string (`evt_<hash>`).
- `node_id`: Stable node identity.
- `node_class`: Hardware profile name.
- `timestamp_domain`: One of the domains above.
- `t_sigma_s`: Declared 1-sigma timing uncertainty.
- `capture_path_bias_s`: Known capture-path delay.
- `tdoa_capable`: Boolean stamped by producer, True only for sub-sample onsets on GPS PPS nodes.
- `corroboration_only`: Boolean, True for Hugbot and presence corroborators.

All TDoA solvers (`hear/solve/point.py`, `hear/backend/associate.py`, `hear/backend/survey.py`) enforce validation against this contract, refusing non-GPS, corroboration-only, or high-bias events at the door.
