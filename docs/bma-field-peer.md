# Offline BMA / DAMA field evidence replay

This is **not a live ingest bridge**. `tools/bma_field_report.py` reads explicit
local JSON files and writes a JSON report to stdout. It makes no network calls,
changes no state and never opens a DAMA pool or store. The existing
`tools/hear_drain.py` pool remains authoritative. HTTPS batch ingest and spooling
are designs, not deployed paths established by this report.

## Inputs and replay

* Use either `--bma` for a JSON result of `hear.ingest.bma_fleet.convert_fleet`,
  or `--fleet` plus `--origin` to run that converter locally. A conversion has
  `observations` and `refusals` arrays. Observations contain identity,
  `source_time_wall_s`, `origin_id`, surveyed `pose`, `bearing_deg_true`,
  `sigma_deg`, `clock_precise: false`, and `evidence: "unvalidated"`.
  Refusals carry `reason`, `ray_index`, `unit_id`, and `det_id`. The converter
  requires its own WGS84/true-north origin and fleet v1 inputs; the origin ID
  is a coordinate reference, **not** a site ID. See its module for the full
  acceptance and refusal contract. A conversion can legitimately have no
  observations; do not treat that as a quiet site.
  In direct mode the converter receives the report's `--as-of-s` and
  `--max-stale-s`; stale/future feeds or rays are retained as refusals, not
  silently accepted. This display-time filter does not establish synchronized
  clocks. The exact fleet snapshot used is also retained in the report.
* `--heartbeat`: the actual `heartbeat.json` written by
  `tools/hear_drain.py` (`write_heartbeat`), whose top-level `sensors` maps
  names to records including `last_success_s`, `last_attempt_s`, `last_error`,
  `last_added`, `uptime_s` and possibly history rings. Records may be partial
  when a sensor never succeeded. This is not a node `/status` document.
* Repeat `--status` for optional saved, raw node `/status` JSON objects,
  each with `node` matching a heartbeat sensor. The `time.valid` flag, when
  present, is reported; the original status including GPS/PPS/clock fields
  is retained. The capture time of these standalone files is unknown.
* Optional `--bma-fleet` supplies the original fleet v1 snapshot: `v: 1`,
  `k: "fleet"`, `origin_id`, `updated_s`, and `units`. The full document is
  retained, including unit clock, `fused`, `not_fused_reason`, quality and
  other recorded degraded state. The converter output itself does not include
  this health information. No association of this snapshot with the conversion
  is proven; the operator must choose the corresponding capture.

All files must be on a local fixed drive; network paths are rejected. Only the
named files are read. Duplicate keys, nonfinite JSON, malformed required fields,
contradictory origin IDs, duplicate statuses and mismatched site labels are
errors. Unknown extra fields are retained rather than stripped. Statuses cannot
add sensors missing from the heartbeat. No input is altered. Exit 0 means the
report was rendered, **not** that the equipment is healthy or validated;
invalid inputs exit 2 with no report on stdout.

For example, with previously saved files (all paths are local):

```powershell
python tools\bma_field_report.py --bma .\converted.json --heartbeat .\heartbeat.json --status .\nyquist-status.json --bma-fleet .\fleet.json --bma-site field-a --dama-site field-a --as-of-s 1795130000 --max-stale-s 7200
```

For end-to-end replay, replace `--bma .\converted.json --bma-fleet .\fleet.json`
with `--fleet .\fleet.json --origin .\origin.json`. The origin document is the
converter's `{origin_id, lat, lon, alt_m, datum: "WGS84", north: "true"}`
contract. It must agree with the fleet origin. Invalid origin or fleet
documents are refused; individual ray refusals remain visible in the report.

Site labels are explicit **operator assertions**: heartbeat and conversion
normally have no site metadata. When any input does contain `site_id`, it must
match the supplied site. This check cannot prove the files came from that site.
Use the same explicit `--as-of-s` for deterministic replay. Freshness is
`as_of_s - last_success_s` for each drain sensor, or `as_of_s - updated_s` for
the fleet snapshot. Missing means unknown, negative means future/clock
inconsistency, and greater than the threshold means stale; equality is fresh.
The default 7200 s matches `hear_drain.py`'s default, but this is not a full
`hear_drain.check()` result: no live ring poll or ring threshold analysis is
performed. Last errors and all rings remain visible even after a recent success.
The status clock flag describes only the saved status; its age is unknown.
Likewise the optional BMA feed age is not a precision-clock guarantee.

## Interpretation and field gate

Each BMA bearing remains **unvalidated**; pose uncertainty and bearing uncertainty
are shown separately, not combined. Fleet `t_wall_s` is a host wall-time estimate,
for display only, not a synchronized arrival time. This tool performs no event
matching, TDoA, localization, or fusion. Side-by-side placement does not imply
that two observations concern the same source. `fused` or other source labels
are recorded claims, not validation by this tool. No sensor is declared offline:
verify DHCP/ARP, node `/status`, and drain ingestion independently, as described
by `tools/fleet.py`.

Before any operational use, capture independently time-stamped field trials at
surveyed locations with known source bearings and independent clock checks,
document poses, mic mapping and uncertainties, include positive and negative
controls, measure bearing errors and clock drift, and record degraded and stale
cases. Review data against agreed acceptance limits; this report cannot set or
pass those limits. Synthetic test fixtures demonstrate software behavior only.

Rollback is to stop invoking the optional report and retain or remove any report
files the operator explicitly created. This tool has no pipeline configuration,
store writes or live bridge to roll back, and cannot undo external captures,
shell redirection, or changes made by other tools. Replaying a report cannot
recover missing evidence or restore drain data.
