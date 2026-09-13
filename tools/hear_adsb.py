#!/usr/bin/env python3
"""ADS-B ingest, geometry, CPA/Doppler and acoustic-flightpath correlation CLI.

    python3 tools/hear_adsb.py probe
    python3 tools/hear_adsb.py track --file capture.json
    python3 tools/hear_adsb.py correlate --file capture.json --observations obs.json

See hear/adsb.py for why this exists (short version: no ADS-B receiver exists in the fleet today,
docs/acoustic-stack.md:586-590) and for every assumption the geometry/Doppler/CPA models make.

Three subcommands:

  probe       Try the LAN/localhost ports a readsb/dump1090/tar1090/ultrafeeder stack commonly
              answers on and report what, if anything, is live. Never raises; "nothing found" is
              itself the answer this command is for.
  track       Ingest state vectors (live URL, a captured aircraft.json file, or an NDJSON replay)
              and, for a chosen ICAO hex (or all of them), print slant range/azimuth/elevation,
              acoustic delay, closest point of approach, and a compact Doppler curve.
  correlate   Ingest state vectors the same way, plus a JSON file of acoustic observations
              (`[{"ts_s": ..., "azimuth_deg": ..., "energy": ...}, ...]`, bearing and energy
              optional), and report every (observation, aircraft) pair inside the configured
              time/bearing window, best match first.

Exactly one of --url / --file / --replay selects the state-vector source; --url with no host
tries `hear.adsb.discover_local_source()` first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear import adsb as ADSB                       # noqa: E402
from hear.backend import survey as SV               # noqa: E402


def _load_state_vectors(a: argparse.Namespace) -> List[ADSB.StateVector]:
    if a.replay:
        svs: List[ADSB.StateVector] = []
        for snapshot in ADSB.replay_ndjson(a.replay):
            svs.extend(snapshot)
        return svs
    if a.file:
        return ADSB.load_aircraft_json_file(a.file)
    url = a.url
    if not url:
        url = ADSB.discover_local_source()
        if url is None:
            print("no --url/--file/--replay given and no local ADS-B source answered the usual "
                  "ports; nothing to ingest", file=sys.stderr)
            return []
        print("discovered live source: %s" % url, file=sys.stderr)
    return ADSB.fetch_aircraft_json(url, timeout=a.timeout)


def _load_survey(a: argparse.Namespace) -> SV.Survey:
    return SV.load_survey(os.path.expanduser(a.survey), min_nodes=1)


def cmd_probe(a: argparse.Namespace) -> int:
    hosts = a.host or None
    url = ADSB.discover_local_source(hosts=hosts, timeout=a.timeout)
    if url is None:
        print("no dump1090/readsb/tar1090/ultrafeeder-style aircraft.json answered on the probed "
              "hosts/ports/paths (ports: %s)" % ", ".join(str(p) for p in ADSB.KNOWN_PORTS))
        return 1
    print("live source: %s" % url)
    return 0


def cmd_track(a: argparse.Namespace) -> int:
    svs = _load_state_vectors(a)
    if a.icao:
        svs = [s for s in svs if s.icao == a.icao.strip().lower()]
    if not svs:
        print("no matching state vectors", file=sys.stderr)
        return 1
    sy = _load_survey(a)
    for sv in svs:
        geo = ADSB.compute_geometry(sv, sy, temp_c=a.temp_c)
        cpa = ADSB.closest_point_of_approach(sv, sy)
        print("%-8s %-10s lat=%.5f lon=%.5f alt_ft=%.0f gs_kts=%.0f track=%.0f"
              % (sv.icao, sv.callsign or "-", sv.lat_deg, sv.lon_deg, sv.alt_ft,
                 sv.ground_speed_kts, sv.track_deg))
        print("  slant_range=%.0fm az=%.1fdeg el=%.1fdeg acoustic_delay=%.2fs"
              % (geo.slant_range_m, geo.azimuth_deg, geo.elevation_deg, geo.acoustic_delay_s))
        print("  CPA: t=%+.1fs range=%.0fm (ts=%.0f)" % (cpa.t_s, cpa.range_m, cpa.ts_unix_s))
        if a.doppler:
            curve = ADSB.doppler_curve(sv, sy, temp_c=a.temp_c,
                                       t_start_s=a.doppler_start_s, t_end_s=a.doppler_end_s,
                                       dt_s=a.doppler_step_s)
            for t_emit, t_arrive, ratio in curve:
                print("    t_emit=%+7.1fs t_arrive=%+7.1fs doppler_ratio=%.4f"
                      % (t_emit, t_arrive, ratio))
    return 0


def _load_observations(path: str) -> List[ADSB.AcousticObservation]:
    with open(path, "r") as fh:
        rows = json.load(fh)
    return [ADSB.AcousticObservation(ts_s=float(r["ts_s"]),
                                    azimuth_deg=(float(r["azimuth_deg"])
                                                if r.get("azimuth_deg") is not None else None),
                                    elevation_deg=(float(r["elevation_deg"])
                                                  if r.get("elevation_deg") is not None else None),
                                    energy=(float(r["energy"])
                                           if r.get("energy") is not None else None))
            for r in rows]


def cmd_correlate(a: argparse.Namespace) -> int:
    svs = _load_state_vectors(a)
    if not svs:
        print("no state vectors to correlate against", file=sys.stderr)
        return 1
    obs = _load_observations(a.observations)
    sy = _load_survey(a)
    matches = ADSB.correlate(obs, svs, sy, temp_c=a.temp_c, time_window_s=a.time_window_s,
                             bearing_window_deg=(None if a.no_bearing_gate
                                                else a.bearing_window_deg))
    if not matches:
        print("no admissible (observation, aircraft) pairs inside the window")
        return 1
    for m in matches:
        bearing = ("bearing_err=%.1fdeg" % m.bearing_error_deg
                  if m.bearing_error_deg is not None else "bearing_err=n/a")
        print("obs@%.1f <-> %-8s %-10s dt=%+.2fs %s score=%.3f"
              % (m.observation.ts_s, m.state_vector.icao, m.state_vector.callsign or "-",
                 m.dt_s, bearing, m.score))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--survey", default=os.path.join(ADSB.repo_root(), "survey.json"),
                    help="node survey JSON; only its origin is used")
    ap.add_argument("--temp-c", type=float, default=20.0)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="look for a live local ADS-B source")
    p_probe.add_argument("--host", action="append", default=[])
    p_probe.add_argument("--timeout", type=float, default=0.5)
    p_probe.set_defaults(func=cmd_probe)

    def _add_source_args(p):
        p.add_argument("--url", default=None, help="live aircraft.json URL; bare use tries "
                                                    "discover_local_source() first")
        p.add_argument("--file", default=None, help="a captured aircraft.json snapshot")
        p.add_argument("--replay", default=None, help="NDJSON of aircraft.json snapshots")
        p.add_argument("--timeout", type=float, default=5.0)

    p_track = sub.add_parser("track", help="geometry/CPA/Doppler for ingested state vectors")
    _add_source_args(p_track)
    p_track.add_argument("--icao", default=None, help="restrict to one ICAO hex")
    p_track.add_argument("--doppler", action="store_true")
    p_track.add_argument("--doppler-start-s", type=float, default=-60.0)
    p_track.add_argument("--doppler-end-s", type=float, default=60.0)
    p_track.add_argument("--doppler-step-s", type=float, default=5.0)
    p_track.set_defaults(func=cmd_track)

    p_corr = sub.add_parser("correlate", help="match acoustic observations to state vectors")
    _add_source_args(p_corr)
    p_corr.add_argument("--observations", required=True,
                        help="JSON list of {ts_s, azimuth_deg?, elevation_deg?, energy?}")
    p_corr.add_argument("--time-window-s", type=float, default=30.0)
    p_corr.add_argument("--bearing-window-deg", type=float, default=20.0)
    p_corr.add_argument("--no-bearing-gate", action="store_true",
                        help="correlate on time alone, e.g. for bare rotor-energy observations "
                             "with no measured bearing")
    p_corr.set_defaults(func=cmd_correlate)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
