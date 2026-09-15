#!/usr/bin/env python3
"""Three-layer fleet health check: OPNsense lease, node /status, hear-drain logs."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Active fleet nodes that are expected to be online.
# NOTE: `puc` (BirdWeather PUC) is NOT included because it has pending/unverified status.
# See hear/nodeclass.py for details on why PUC nodes require GPS PPS verification before
# being admitted as an arrival source.
EXPECTED_NODES: Tuple[str, ...] = (
    "gold",
    "nyquist",
    "mach",
    "rankine",
    "ageev",
    "kasami",
    "fancyantsy",
    "financialdistress",
    "myasshurts",
)

DEPLOYED_DEFAULT_IPS: Dict[str, str] = {
    "nyquist": "172.16.100.105",
    "mach": "172.16.100.116",
    "rankine": "172.16.100.50",
    "gold": "172.16.100.82",
    "ageev": "172.16.100.83",
    "kasami": "172.16.100.90",
}

DEFAULT_OPNSENSE = "https://172.16.100.1"
DEFAULT_DHCP_ENDPOINT = "/api/dhcpv4/leases/searchLease/"
DEFAULT_NAMESPACE = "dama"
DEFAULT_SECRET = "opn-agent-secrets"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_S = 1.5
DEFAULT_SINCE = "2h"
DEFAULT_POOL = "~/hear-pool"
DEFAULT_DATA_WINDOW_S = 7200.0
KUBECTL_HEARTBEAT_READ_LIMIT_B = 1_048_576
POOL_HEARTBEATS = {
    "drain": "heartbeat.json",
    "score": "state/score_heartbeat.json",
    "tag": "state/tag_heartbeat.json",
}
PMTK_STATUS_CLASSES: Tuple[str, ...] = (
    "esp32s3-i2s-gps",
    "esp32s3-speaker",
    "esp32s3-box3",
    "puc-pps",
    "puc-ntp",
)
NO_GPS_STATUS_CLASSES: Tuple[str, ...] = ("puc-ntp",)
NO_PPS_STATUS_CLASSES: Tuple[str, ...] = ("puc-ntp",)
NO_SD_STATUS_CLASSES: Tuple[str, ...] = ("esp32s3-i2s-gps",)

ENV_CREDENTIAL_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("OPNSENSE_USERNAME", "OPNSENSE_PASSWORD"),
    ("OPNSENSE_USER", "OPNSENSE_PASS"),
    ("OPN_USERNAME", "OPN_PASSWORD"),
    ("OPN_USER", "OPN_PASS"),
    ("OPN_AGENT_USERNAME", "OPN_AGENT_PASSWORD"),
    ("OPN_AGENT_USER", "OPN_AGENT_PASS"),
)


@dataclass
class WarningSink:
    items: List[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        if text not in self.items:
            self.items.append(text)


@dataclass
class NodeResult:
    node: str
    ip: str = "-"
    network: str = "FAIL"
    http: str = "FAIL"
    http_ok: bool = False
    state: str = "offline"
    gps: str = "?"
    ingestion: str = "FAIL"
    uptime: str = "?"
    pps: str = "?"
    utc: str = "?"
    rssi: str = "?"
    dets: str = "?"
    temp: str = "?"
    details: List[str] = field(default_factory=list)

    def ok(self) -> bool:
        return self.ip != "-" and self.http_ok and self.ingestion == "OK"


@dataclass
class HttpResult:
    code: Optional[int]
    data: Optional[Dict[str, Any]]
    error: Optional[str]


@dataclass
class IngestionEvidence:
    sketch: bool = False
    scene: bool = False
    detail: List[str] = field(default_factory=list)

    def ok(self) -> bool:
        return self.sketch and self.scene


# ---------------------------------------------------------------------------
# General helpers


def _norm_name(name: str) -> str:
    return name.strip().lower().rstrip(".").split(".")[0]


def _repo_default_survey() -> Path:
    return Path(__file__).resolve().parents[1] / "survey.json"


def _json_scalar(value: Any) -> str:
    if value is None:
        return "?"
    if isinstance(value, bool):
        return "yes" if value else "NO"
    return str(value)


def _nested(d: Mapping[str, Any], *path: str) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def _first(d: Mapping[str, Any], paths: Iterable[Tuple[str, ...]]) -> Any:
    for path in paths:
        value = _nested(d, *path)
        if value is not None:
            return value
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "UNKNOWN"
    if seconds < 0:
        seconds = 0.0
    if seconds < 90:
        return "%.0fs" % seconds
    if seconds < 36 * 3600:
        return "%.1fh" % (seconds / 3600.0)
    return "%.1fd" % (seconds / 86400.0)


def _age(now: float, epoch_s: Any) -> Optional[float]:
    v = _as_float(epoch_s)
    return None if v is None or v <= 0 else max(0.0, now - v)


def _health(value: Any, *, healthy: bool = False, reason: Optional[str] = None,
            not_applicable: bool = False) -> Dict[str, Any]:
    if not_applicable:
        state = "not_applicable"
    elif value is None:
        state = "unknown"
    else:
        state = "healthy" if healthy else "unhealthy"
    out = {"state": state, "value": value}
    if reason:
        out["reason"] = reason
    return out


def _load_json_file(path: Optional[str], warnings: WarningSink) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    p = Path(os.path.expanduser(path))
    if not p.exists():
        warnings.add("data source not found: %s" % p)
        return None
    try:
        payload = json.loads(p.read_text())
    except Exception as exc:
        warnings.add("could not read JSON data source %s: %s" % (p, exc))
        return None
    if not isinstance(payload, dict):
        warnings.add("JSON data source %s is %s, not an object" % (p, type(payload).__name__))
        return None
    return payload


def _read_pool_heartbeat(pool: str, rel: str, warnings: WarningSink) -> Optional[Dict[str, Any]]:
    return _load_json_file(str(Path(os.path.expanduser(pool)) / rel), warnings)


def _kubectl_exec_json(namespace: str, target: str, path: str, warnings: WarningSink,
                       timeout: float, limit_b: int = KUBECTL_HEARTBEAT_READ_LIMIT_B
                       ) -> Optional[Dict[str, Any]]:
    """Read one bounded JSON file through an existing pod/deployment; never scans the PVC."""
    if not shutil.which("kubectl"):
        warnings.add("kubectl is unavailable; cannot read %s from %s" % (path, target))
        return None
    cmd = ["kubectl", "-n", namespace, "exec", target, "--",
           "head", "-c", str(int(limit_b)), path]
    try:
        proc = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except Exception as exc:
        warnings.add("kubectl exec failed for %s:%s: %s" % (target, path, exc))
        return None
    if proc.returncode != 0:
        warnings.add("kubectl exec failed for %s:%s: %s"
                     % (target, path, (proc.stderr or proc.stdout).strip()))
        return None
    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        warnings.add("kubectl exec returned invalid JSON for %s:%s: %s" % (target, path, exc))
        return None
    if not isinstance(payload, dict):
        warnings.add("kubectl exec JSON for %s:%s is %s, not an object"
                     % (target, path, type(payload).__name__))
        return None
    return payload


def _kubectl_http_json(namespace: str, target: str, url: str, warnings: WarningSink,
                       timeout: float, limit_b: int = KUBECTL_HEARTBEAT_READ_LIMIT_B
                       ) -> Optional[Dict[str, Any]]:
    if not shutil.which("kubectl"):
        warnings.add("kubectl is unavailable; cannot read %s from %s" % (url, target))
        return None
    code = (
        "import json,sys,urllib.request;"
        "r=urllib.request.urlopen(sys.argv[1],timeout=%r);"
        "sys.stdout.write(r.read(%d).decode('utf-8'))" % (float(timeout), int(limit_b))
    )
    cmd = ["kubectl", "-n", namespace, "exec", target, "--", "python3", "-c", code, url]
    try:
        proc = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except Exception as exc:
        warnings.add("kubectl exec HTTP read failed for %s %s: %s" % (target, url, exc))
        return None
    if proc.returncode != 0:
        warnings.add("kubectl exec HTTP read failed for %s %s: %s"
                     % (target, url, (proc.stderr or proc.stdout).strip()))
        return None
    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        warnings.add("kubectl exec HTTP returned invalid JSON for %s %s: %s" % (target, url, exc))
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# Credentials and OPNsense leases


def env_credentials() -> Optional[Tuple[str, str, str]]:
    for user_key, pass_key in ENV_CREDENTIAL_PAIRS:
        user = os.environ.get(user_key)
        password = os.environ.get(pass_key)
        if user and password:
            return user, password, "env:%s/%s" % (user_key, pass_key)
    return None


def _decode_secret_value(data: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        raw = data.get(key)
        if not raw:
            continue
        if not isinstance(raw, str):
            continue
        try:
            return base64.b64decode(raw).decode("utf-8").strip()
        except Exception:
            return None
    return None


def kubectl_secret_credentials(namespace: str, secret: str, warnings: WarningSink,
                               timeout: float = 20.0) -> Optional[Tuple[str, str, str]]:
    if not shutil.which("kubectl"):
        warnings.add("kubectl is unavailable; cannot read %s/%s for OPNsense credentials"
                     % (namespace, secret))
        return None
    # Try specified namespace first, then fall back to opnsense-copilot if different
    namespaces_to_try = [namespace]
    if namespace != "opnsense-copilot":
        namespaces_to_try.append("opnsense-copilot")

    payload = None
    used_ns = None
    for ns in namespaces_to_try:
        cmd = ["kubectl", "-n", ns, "get", "secret", secret, "-o", "json"]
        try:
            proc = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=timeout)
        except Exception as exc:
            continue
        if proc.returncode == 0:
            try:
                payload = json.loads(proc.stdout)
                used_ns = ns
                break
            except json.JSONDecodeError:
                continue

    if not payload:
        warnings.add("kubectl could not read secret %s in namespaces %s" % (secret, namespaces_to_try))
        return None

    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        warnings.add("kubectl secret %s/%s has no data map" % (used_ns, secret))
        return None
    user = _decode_secret_value(data, ("username", "user", "OPNSENSE_API_KEY"))
    password = _decode_secret_value(data, ("password", "pass", "OPNSENSE_API_SECRET"))
    if not user or not password:
        warnings.add("kubectl secret %s/%s lacks username/user/OPNSENSE_API_KEY and password/pass/OPNSENSE_API_SECRET keys"
                     % (used_ns, secret))
        return None
    return user, password, "secret:%s/%s" % (used_ns, secret)


def get_credentials(namespace: str, secret: str, warnings: WarningSink) -> Optional[Tuple[str, str, str]]:
    creds = env_credentials()
    if creds:
        return creds
    return kubectl_secret_credentials(namespace, secret, warnings)


def _join_url(base: str, endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    return base.rstrip("/") + "/" + endpoint.lstrip("/")


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    body = ""
    try:
        body = exc.read(512).decode("utf-8", "replace").strip()
    except Exception:
        body = ""
    return "HTTP %s %s%s" % (exc.code, exc.reason, (": " + body) if body else "")


def query_opnsense_leases(base: str, endpoint: str, creds: Tuple[str, str, str], timeout: float,
                          verify_tls: bool, warnings: WarningSink) -> List[Dict[str, Any]]:
    url = _join_url(base, endpoint)
    user, password, source = creds
    token = base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
    body = json.dumps({"current": 1, "rowCount": 1000, "sort": {}, "searchPhrase": ""}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Basic " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "dama-check-fleet-health/1",
        },
    )
    context = None
    if url.startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError("OPNsense DHCP API %s failed using %s: %s"
                           % (url, source, _http_error_message(exc))) from exc
    except Exception as exc:
        raise RuntimeError("OPNsense DHCP API %s failed using %s: %s" % (url, source, exc)) from exc
    return parse_opnsense_leases(payload, warnings)


def _rows_from_payload(payload: Any) -> List[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        raise ValueError("DHCP response is %s, not a JSON object/list" % type(payload).__name__)
    for key in ("rows", "leases", "data", "items"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return rows
    raise ValueError("DHCP response has no rows/leases/data/items list")


def parse_opnsense_leases(payload: Any, warnings: Optional[WarningSink] = None) -> List[Dict[str, Any]]:
    if warnings is None:
        warnings = WarningSink()
    rows = _rows_from_payload(payload)
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        host = _first(row, (("hostname",), ("host",), ("name",), ("client-hostname",)))
        ip = _first(row, (("address",), ("ip",), ("ipaddr",), ("ipAddress",), ("ipv4",)))
        if not ip:
            continue
        out.append({
            "hostname": str(host or "").strip(),
            "address": str(ip).strip(),
            "state": str(_first(row, (("state",), ("status",), ("binding_state",))) or ""),
            "mac": str(_first(row, (("mac",), ("hwaddr",), ("macAddress",))) or ""),
        })
    if not out:
        warnings.add("OPNsense DHCP API returned no usable lease rows")
    return out


def find_lease_by_hostname(leases: Sequence[Mapping[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    norm = _norm_name(name)
    if not norm:
        return None
    for lease in leases:
        host = _norm_name(str(lease.get("hostname") or ""))
        if host and host == norm:
            return dict(lease)
    return None


def resolve_hosts(targets: Sequence[str], leases: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    ip_map: Dict[str, str] = {}
    for target in targets:
        if "=" in target or target.startswith("http://") or target.startswith("https://"):
            continue
        lease = find_lease_by_hostname(leases, target)
        if lease and lease.get("address"):
            ip_map[_norm_name(target)] = str(lease["address"])
    return ip_map


def split_target(target: str, resolved_map: Optional[Mapping[str, str]] = None) -> Tuple[str, str]:
    if target.startswith("http://") or target.startswith("https://"):
        return _norm_name(target.split("//")[1].split("/")[0]), target if target.endswith("/status") else target.rstrip("/") + "/status"
    if "=" in target:
        name, ip = target.split("=", 1)
        return _norm_name(name), "http://%s/status" % ip.strip()
    norm = _norm_name(target)
    if resolved_map and norm in resolved_map:
        return norm, "http://%s/status" % resolved_map[norm]
    return norm, "http://%s.local/status" % norm


def parse_status(d: Mapping[str, Any]) -> Dict[str, Any]:
    node_class = _first(d, (("class",), ("node_class",)))
    fix = _first(d, (("gps", "fix"), ("gps_fix",), ("fix",)))
    sats = _first(d, (("gps", "sats"), ("gps", "satellites"), ("gps_sats",), ("sats",)))
    tacc_ns = _first(d, (("gps", "tacc_ns"), ("gps_tacc_ns"), ("tacc_ns",)))
    edges = _first(d, (("pps", "edges"), ("pps_edges",)))
    spread_us = _first(d, (("pps", "spread_us"), ("pps_spread_us",)))
    glitches = _first(d, (("pps", "glitches"), ("pps_glitches",)))
    utc = _first(d, (("time", "valid"), ("time", "synced"), ("time_sync",), ("utc_valid",)))
    clock_state = _first(d, (("time", "state"), ("clock_state",)))
    rssi = _first(d, (("net", "rssi"), ("wifi", "rssi"), ("rssi",)))
    sd = _first(d, (("sd",),))
    sd_free_mb = _first(d, (("sd_free_mb",),))
    mic = _first(d, (("selftest", "mic"), ("mic",)))
    mic_state = _first(d, (("selftest", "mic_state"), ("mic_state",)))
    mic_reason = _first(d, (("selftest", "mic_reason"), ("mic_reason",)))
    return {
        "class": node_class,
        "fix": fix,
        "sats": sats,
        "tacc_ns": tacc_ns,
        "pps_edges": edges,
        "pps_spread_us": spread_us,
        "pps_glitches": glitches,
        "time_valid": utc,
        "clock_state": clock_state,
        "rssi": rssi,
        "sd": sd,
        "sd_free_mb": sd_free_mb,
        "mic": mic,
        "mic_state": mic_state,
        "mic_reason": mic_reason,
    }


def _status_class(p: Mapping[str, Any]) -> str:
    return _norm_name(str(p.get("class") or ""))


def _gps_expected(p: Mapping[str, Any]) -> bool:
    return _status_class(p) not in NO_GPS_STATUS_CLASSES


def _pps_expected(p: Mapping[str, Any]) -> bool:
    return _status_class(p) not in NO_PPS_STATUS_CLASSES


def _sd_expected(p: Mapping[str, Any]) -> bool:
    return _status_class(p) not in NO_SD_STATUS_CLASSES


def _gps_proto_label(p: Mapping[str, Any]) -> str:
    if not _gps_expected(p):
        return "NOGPS"
    return "PMTK" if _status_class(p) in PMTK_STATUS_CLASSES else "UBX"


def _gps_summary(p: Mapping[str, Any]) -> str:
    return "%s:%s/%s" % (_gps_proto_label(p), _json_scalar(p.get("fix")), _json_scalar(p.get("sats")))


def _gps_fix_ok(p: Mapping[str, Any]) -> bool:
    # `gps.fix` is protocol-specific: PMTK boards report NMEA GGA fix quality (1 = a live fix)
    # while UBX boards report u-blox fixType (3 = 3D). One shared `< 3` rule marks every healthy
    # PMTK node degraded, which is exactly the bug PR #135 fixed in the firmware self-test.
    if not _gps_expected(p):
        return True
    fix = p.get("fix")
    if fix is None:
        return False
    try:
        n = int(fix)
    except (TypeError, ValueError):
        return False
    node_class = _status_class(p)
    return n >= 1 if node_class in PMTK_STATUS_CLASSES else n >= 3


def _mic_issue(p: Mapping[str, Any]) -> Optional[str]:
    state = p.get("mic_state")
    legacy = p.get("mic")
    reason = p.get("mic_reason")
    if state in {"quiet", "normal"}:
        return None
    if state in {"capture-failure", "stuck", "floating", "saturated"}:
        detail = "mic=%s" % state
        if legacy and legacy != state:
            detail += " (legacy %s)" % legacy
        if reason:
            detail += ": %s" % reason
        return detail
    if legacy and legacy != "ok":
        return "mic=%s" % legacy
    return None


def evaluate_health(target_name: str, status_data: Optional[Mapping[str, Any]],
                    err: Optional[Exception] = None) -> Dict[str, Any]:
    if status_data is None:
        reason = str(err) if err else "unreachable"
        return {"state": "offline", "reasons": [reason], "node": target_name}

    reasons: List[str] = []
    p = parse_status(status_data)

    if _gps_expected(p) and not _gps_fix_ok(p):
        reasons.append("fix=%s" % p["fix"])
    if p.get("clock_state") not in (None, "", "LOCKED"):
        reasons.append("clock=%s" % p["clock_state"])
    elif p["time_valid"] is not True:
        reasons.append("no UTC anchor")
    if _pps_expected(p) and (p["pps_edges"] is None or p["pps_edges"] == 0):
        reasons.append("timebase never locked")
    if _pps_expected(p) and p["pps_glitches"] and p["pps_glitches"] > 0:
        reasons.append("%d pps glitch(es)" % p["pps_glitches"])
    mic_issue = _mic_issue(p)
    if mic_issue:
        reasons.append(mic_issue)
    if p["rssi"] is not None and p["rssi"] < -80:
        reasons.append("rssi=%d dBm" % p["rssi"])
    if _sd_expected(p) and p["sd"] is False:
        reasons.append("no SD card")
    elif _sd_expected(p) and p["sd_free_mb"] is not None and p["sd_free_mb"] < 100:
        reasons.append("%d MB free" % p["sd_free_mb"])

    reported_node = str(status_data.get("node") or target_name)
    state = "degraded" if reasons else "online"
    return {"state": state, "reasons": reasons, "node": reported_node}


def _lease_score(lease: Mapping[str, Any]) -> int:
    state = str(lease.get("state") or "").lower()
    if any(word in state for word in ("active", "online", "bound")):
        return 3
    if any(word in state for word in ("expired", "inactive", "free", "offline")):
        return 1
    return 2


def dhcp_ip_map(nodes: Sequence[str], leases: Sequence[Mapping[str, Any]],
                warnings: WarningSink) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for node in nodes:
        matches = [lease for lease in leases if _norm_name(str(lease.get("hostname") or "")) == node]
        if not matches:
            continue
        matches.sort(key=_lease_score, reverse=True)
        chosen = matches[0]
        out[node] = str(chosen["address"])
        if len({str(m.get("address")) for m in matches}) > 1:
            warnings.add("multiple DHCP leases matched %s; chose %s" % (node, chosen["address"]))
    return out


# ---------------------------------------------------------------------------
# Survey/default fallback map


def load_fallback_ip_map(path: Path, warnings: WarningSink) -> Dict[str, str]:
    out = dict(DEPLOYED_DEFAULT_IPS)
    if not path.exists():
        warnings.add("survey fallback file not found: %s" % path)
        return out
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        warnings.add("could not read survey fallback %s: %s" % (path, exc))
        return out
    if not isinstance(payload, Mapping):
        warnings.add("survey fallback %s is not a JSON object" % path)
        return out

    ip_map = payload.get("ip_map")
    if isinstance(ip_map, Mapping):
        for name, ip in ip_map.items():
            if name and ip:
                out[_norm_name(str(name))] = str(ip).strip()

    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        for row in nodes:
            if not isinstance(row, Mapping):
                continue
            name = _first(row, (("name",), ("node",), ("hostname",), ("id",)))
            ip = _first(row, (("ip",), ("address",), ("host",)))
            if name and ip:
                out[_norm_name(str(name))] = str(ip).strip()
    return out


def resolve_node_ips(nodes: Sequence[str], dhcp: Mapping[str, str], fallback: Mapping[str, str]) -> Dict[str, Tuple[str, str]]:
    resolved: Dict[str, Tuple[str, str]] = {}
    for node in nodes:
        if node in dhcp:
            resolved[node] = (dhcp[node], "DHCP")
        elif node in fallback:
            resolved[node] = (fallback[node], "FALLBACK")
        else:
            resolved[node] = ("-", "FAIL")
    return resolved


# ---------------------------------------------------------------------------
# Node /status probe


def fetch_status(target: str, timeout: float = DEFAULT_TIMEOUT_S, retries: int = DEFAULT_RETRIES, backoff: float = DEFAULT_BACKOFF_S) -> HttpResult:
    if target.startswith("http://") or target.startswith("https://"):
        url = target if target.endswith("/status") else target.rstrip("/") + "/status"
    else:
        url = "http://%s/status" % target

    last_error: Optional[str] = None
    last_code: Optional[int] = None
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "dama-check-fleet-health/1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                code = int(resp.getcode())
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception as exc:
                    return HttpResult(code, None, "invalid JSON: %s" % exc)
                if not isinstance(data, dict):
                    return HttpResult(code, None, "JSON status is %s, not an object" % type(data).__name__)
                return HttpResult(code, data, None)
        except urllib.error.HTTPError as exc:
            last_code = int(exc.code)
            last_error = _http_error_message(exc)
        except Exception as exc:
            last_error = str(exc)
        if attempt < max(1, retries) - 1:
            time.sleep(backoff)
    return HttpResult(last_code, None, last_error or "unreachable")


def apply_status_fields(result: NodeResult, http: HttpResult) -> None:
    if http.code is not None:
        result.http = str(http.code)
    elif http.error:
        result.http = "FAIL"
    if http.error:
        result.details.append(http.error)
    d = http.data
    if not d:
        return

    uptime = _first(d, (("uptime_s",), ("uptime",), ("uptimeSeconds",)))
    uptime_ms = _first(d, (("uptime_ms",), ("uptimeMillis",)))
    if uptime is None and uptime_ms is not None:
        ms = _as_float(uptime_ms)
        uptime = None if ms is None else ms / 1000.0
    upf = _as_float(uptime)
    result.uptime = "?" if upf is None else "%.0fs" % upf

    # Show the protocol beside the raw fix number so a PMTK `1` is not read against a UBX `3`
    # as if they were the same quality scale.
    result.gps = _gps_summary(parse_status(d))

    edges = _first(d, (("pps", "edges"), ("pps_edges",)))
    glitches = _first(d, (("pps", "glitches"), ("pps_glitches",)))
    synced = _first(d, (("pps", "synced"), ("pps", "sync"), ("pps_sync",)))
    if edges is not None or glitches is not None:
        result.pps = "e%s/g%s" % (_json_scalar(edges), _json_scalar(glitches))
    elif synced is not None:
        result.pps = _json_scalar(synced)

    utc = _first(d, (("time", "valid"), ("time", "synced"), ("time_sync",), ("utc_valid",)))
    result.utc = _json_scalar(utc)

    rssi = _first(d, (("net", "rssi"), ("wifi", "rssi"), ("rssi",)))
    result.rssi = _json_scalar(rssi)

    dets = _first(d, (("audio", "detections"), ("audio", "dets"), ("detections",), ("dets",)))
    result.dets = _json_scalar(dets)

    temp = _first(d, (("temperature_c",), ("temp_c",), ("temperature",),
                      ("env", "temperature_c"), ("env", "temp_c"),
                      ("bme", "temperature_c"), ("bme", "temp_c")))
    temp_f = _as_float(temp)
    result.temp = "?" if temp_f is None else "%.1fC" % temp_f


# ---------------------------------------------------------------------------
# hear-drain log verification


def _run_kubectl(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["kubectl", *args], check=False, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)


def _pod_sort_key(item: Mapping[str, Any]) -> str:
    status = item.get("status") if isinstance(item.get("status"), Mapping) else {}
    meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    return str(status.get("startTime") or meta.get("creationTimestamp") or "")


def find_hear_drain_pods(namespace: str, warnings: WarningSink, limit: int,
                         timeout: float = 20.0) -> List[str]:
    if not shutil.which("kubectl"):
        warnings.add("kubectl is unavailable; cannot verify hear-drain logs")
        return []
    try:
        proc = _run_kubectl(["-n", namespace, "get", "pods", "-l", "app=hear-drain", "-o", "json"], timeout)
    except Exception as exc:
        warnings.add("kubectl pod lookup failed: %s" % exc)
        return []
    if proc.returncode != 0:
        warnings.add("kubectl could not list hear-drain pods in %s: %s"
                     % (namespace, (proc.stderr or proc.stdout).strip()))
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        warnings.add("kubectl pod list returned invalid JSON: %s" % exc)
        return []
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list) or not items:
        warnings.add("no pods found with label app=hear-drain in namespace %s" % namespace)
        return []
    items = sorted([i for i in items if isinstance(i, Mapping)], key=_pod_sort_key, reverse=True)
    names = []
    for item in items[:max(1, limit)]:
        meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        name = meta.get("name")
        if name:
            names.append(str(name))
    return names


def find_pool_reader_pod(namespace: str, warnings: WarningSink,
                         timeout: float = 20.0) -> Optional[str]:
    """Find one running pod that already mounts the hear-pool PVC for bounded JSON reads."""
    if not shutil.which("kubectl"):
        warnings.add("kubectl is unavailable; cannot read pool heartbeats")
        return None
    labels = ("hear-annotate", "hear-tdoa", "hear-score", "hear-tag", "hear-drain")
    selector = "app in (%s)" % ",".join(labels)
    try:
        proc = _run_kubectl(["-n", namespace, "get", "pods", "-l", selector, "-o", "json"],
                            timeout)
    except Exception as exc:
        warnings.add("kubectl pool-reader lookup failed: %s" % exc)
        return None
    if proc.returncode != 0:
        warnings.add("kubectl could not list pool-reader pods in %s: %s"
                     % (namespace, (proc.stderr or proc.stdout).strip()))
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        warnings.add("kubectl pool-reader list returned invalid JSON: %s" % exc)
        return None
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return None
    running = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status") if isinstance(item.get("status"), Mapping) else {}
        meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        name = meta.get("name")
        if name and status.get("phase") == "Running":
            running.append(item)
    if not running:
        warnings.add("no running hear-pool reader pod found")
        return None
    running.sort(key=_pod_sort_key, reverse=True)
    meta = running[0].get("metadata") if isinstance(running[0].get("metadata"), Mapping) else {}
    return str(meta.get("name"))


def read_hear_drain_logs(namespace: str, since: str, pods: Sequence[str], warnings: WarningSink,
                         timeout: float = 45.0) -> str:
    chunks: List[str] = []
    for pod in pods:
        args = ["-n", namespace, "logs", pod, "--since", since, "--all-containers=true"]
        try:
            proc = _run_kubectl(args, timeout)
        except Exception as exc:
            warnings.add("kubectl logs failed for %s: %s" % (pod, exc))
            continue
        if proc.returncode != 0:
            warnings.add("kubectl logs failed for %s: %s" % (pod, (proc.stderr or proc.stdout).strip()))
            continue
        if proc.stdout.strip():
            chunks.append("\n# pod %s\n%s" % (pod, proc.stdout))
    if not chunks:
        warnings.add("no hear-drain logs were available for --since %s" % since)
    return "\n".join(chunks)


def _node_pattern(nodes: Sequence[str]) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_-])(%s)(?![A-Za-z0-9_-])"
                      % "|".join(re.escape(n) for n in sorted(nodes, key=len, reverse=True)),
                      re.IGNORECASE)


def _line_nodes(line: str, pat: re.Pattern[str]) -> List[str]:
    return sorted({_norm_name(m.group(1)) for m in pat.finditer(line)})


def _has_positive_number(pattern: str, line: str) -> bool:
    match = re.search(pattern, line, re.IGNORECASE)
    if not match:
        return False
    try:
        return int(match.group(1)) > 0
    except (TypeError, ValueError):
        return False


def _has_nonnegative_number(pattern: str, line: str) -> bool:
    """Match a pattern with a non-negative number (0 or greater)."""
    match = re.search(pattern, line, re.IGNORECASE)
    if not match:
        return False
    try:
        return int(match.group(1)) >= 0
    except (TypeError, ValueError):
        return False


def classify_ingestion_line(line: str) -> Tuple[bool, bool, Optional[str]]:
    lower = line.lower()
    sketch = False
    scene = False
    reason: Optional[str] = None

    if _has_nonnegative_number(r"\+(\d+)\s+sketch\b", lower):
        sketch, reason = True, "+sketch"
    if _has_nonnegative_number(r"\+(\d+)\s+record\b", lower):
        sketch, reason = True, "+record"
    if "dets" in lower and ("row(s)" in lower or re.search(r"\brows?\b", lower)):
        sketch, reason = True, "dets rows"
    if "sketches-" in lower and ("added" in lower or "row" in lower or "decoded" in lower):
        sketch, reason = True, "sketch jsonl"

    if _has_nonnegative_number(r"\+(\d+)\s+scene\b", lower):
        scene, reason = True, "+scene"
    if "scene" in lower and ("row(s)" in lower or re.search(r"\brows?\b", lower)):
        scene, reason = True, "scene rows"

    return sketch, scene, reason


def parse_ingestion_evidence(logs: str, nodes: Sequence[str]) -> Tuple[Dict[str, IngestionEvidence], IngestionEvidence, bool]:
    per_node: Dict[str, IngestionEvidence] = {node: IngestionEvidence() for node in nodes}
    global_ev = IngestionEvidence()
    node_pat = _node_pattern(nodes)
    saw_node_named = False
    current_nodes: List[str] = []

    for raw in logs.splitlines():
        line = raw.strip()
        if not line:
            continue
        named = _line_nodes(line, node_pat)
        if named:
            current_nodes = named
            saw_node_named = True
        sketch, scene, reason = classify_ingestion_line(line)
        if not sketch and not scene:
            continue
        target_nodes = named or (current_nodes if raw[:1].isspace() else [])
        # Reset current_nodes if this is a non-indented line with no named nodes
        if not named and not raw[:1].isspace():
            current_nodes = []
        if target_nodes:
            saw_node_named = True
            for node in target_nodes:
                ev = per_node.setdefault(node, IngestionEvidence())
                ev.sketch = ev.sketch or sketch
                ev.scene = ev.scene or scene
                if reason and reason not in ev.detail:
                    ev.detail.append(reason)
        else:
            global_ev.sketch = global_ev.sketch or sketch
            global_ev.scene = global_ev.scene or scene
            if reason and reason not in global_ev.detail:
                global_ev.detail.append(reason)
    return per_node, global_ev, saw_node_named


def apply_ingestion(results: Dict[str, NodeResult], logs: str, warnings: WarningSink) -> None:
    if not logs.strip():
        for r in results.values():
            r.ingestion = "FAIL"
            r.details.append("no hear-drain log evidence")
        return

    per_node, global_ev, saw_node_named = parse_ingestion_evidence(logs, list(results))
    if not saw_node_named and global_ev.ok():
        for r in results.values():
            r.ingestion = "OK"
            r.details.append("global ingestion evidence")
        return

    for node, r in results.items():
        ev = per_node.get(node, IngestionEvidence())
        if ev.ok():
            r.ingestion = "OK"
            if ev.detail:
                r.details.append("ingest:%s" % ",".join(ev.detail))
        else:
            missing = []
            if not ev.sketch:
                missing.append("sketch/dets/record")
            if not ev.scene:
                missing.append("scene")
            if global_ev.ok() and saw_node_named:
                r.details.append("global evidence ignored because logs named nodes")
            r.details.append("missing ingest evidence: %s" % "+".join(missing))


# ---------------------------------------------------------------------------
# Data-state report


def _status_clock(status: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not status:
        return _health(None, reason="no /status document")
    t = status.get("time") if isinstance(status.get("time"), Mapping) else {}
    state = _first(status, (("time", "state"), ("clock_state",)))
    if state is None:
        valid = _first(status, (("time", "valid"), ("time", "synced"), ("utc_valid",)))
        state = "LOCKED" if valid is True else ("FAULT" if valid is False else None)
    return _health(state, healthy=str(state).upper() == "LOCKED",
                   reason=None if state is not None else "missing time.state/time.valid")


def _status_anchor_age(status: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not status:
        return _health(None, reason="no /status document")
    raw = _first(status, (("time", "anchor_age_us"), ("anchor_age_us",)))
    us = _as_float(raw)
    if us is None:
        return _health(None, reason="status does not expose anchor_age_us")
    return _health(us / 1_000_000.0, healthy=us >= 0)


def _status_mic(status: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not status:
        return _health(None, reason="no /status document")
    parsed = parse_status(status)
    value = parsed.get("mic_state") or parsed.get("mic")
    issue = _mic_issue(parsed)
    return _health(value, healthy=issue is None and value is not None, reason=issue)


def _sum_recent(ring: Any, now: float, window_s: float, fields: Sequence[str]) -> Dict[str, Any]:
    out = {f: 0 for f in fields}
    out["runs"] = 0
    out["unknown_runs"] = 0
    if not isinstance(ring, list):
        out["state"] = "unknown"
        return out
    for e in ring:
        if not isinstance(e, Mapping):
            continue
        at = _as_float(e.get("at"))
        if at is None or at < now - window_s:
            continue
        out["runs"] += 1
        if e.get("unknown"):
            out["unknown_runs"] += 1
        for f in fields:
            if e.get(f) is not None:
                out[f] += int(e.get(f) or 0)
    out["state"] = "healthy" if out["runs"] else "unknown"
    return out


def _drain_node_report(node: str, sensor: Mapping[str, Any], now: float,
                       window_s: float) -> Dict[str, Any]:
    age = _age(now, sensor.get("last_success_s"))
    clips = _sum_recent(sensor.get("clips_recent"), now, window_s,
                        ("seen", "fetched", "gone", "deferred", "probed_404"))
    live = _sum_recent(sensor.get("live_recent"), now, window_s,
                       ("rows", "added", "lost"))
    unfetched = _sum_recent(sensor.get("unfetched_recent"), now, window_s, ("bytes",))
    return {
        "node": node,
        "kind": sensor.get("kind"),
        "last_success_age_s": age,
        "last_error": sensor.get("last_error"),
        "last_detection_added": sensor.get("last_added"),
        "scene": {
            "unfetched_bytes_window": unfetched.get("bytes"),
            "unknown_runs": unfetched.get("unknown_runs"),
            "last_reason": sensor.get("last_unfetched_reason"),
            "state": unfetched.get("state"),
        },
        "clips": {
            "named_window": clips.get("seen"),
            "fetched_window": clips.get("fetched"),
            "destroyed_window": clips.get("gone"),
            "deferred_window": clips.get("deferred"),
            "unknown_runs": clips.get("unknown_runs"),
            "state": clips.get("state"),
        },
        "live_ring": {
            "rows_window": live.get("rows"),
            "added_window": live.get("added"),
            "lost_window": live.get("lost"),
            "pending": None,
            "state": live.get("state"),
        },
    }


def _workflow_report(hb: Optional[Mapping[str, Any]], now: float, window_s: float,
                     count_fields: Sequence[str]) -> Dict[str, Any]:
    if not hb:
        return {"state": "unknown", "reason": "heartbeat missing", "last_run_age_s": None,
                "window_runs": 0}
    runs = hb.get("runs") or []
    if not isinstance(runs, list) or not runs:
        return {"state": "unknown", "reason": "heartbeat records no runs",
                "last_run_age_s": _age(now, hb.get("last_run_s")), "window_runs": 0}
    last = runs[-1] if isinstance(runs[-1], Mapping) else {}
    window = [r for r in runs if isinstance(r, Mapping)
              and (_as_float(r.get("at")) or 0.0) >= now - window_s] or [last]
    totals = {f: sum(int(r.get(f) or 0) for r in window) for f in count_fields}
    refused: Dict[str, int] = {}
    by_node: Dict[str, Dict[str, int]] = {}
    by_source: Dict[str, int] = {}
    for r in window:
        for k, v in (r.get("by_reason") or {}).items():
            refused[k] = refused.get(k, 0) + int(v)
        for k, v in (r.get("by_source") or {}).items():
            by_source[k] = by_source.get(k, 0) + int(v)
        for n, vals in (r.get("by_node") or {}).items():
            bucket = by_node.setdefault(n, {})
            if isinstance(vals, Mapping):
                for k, v in vals.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        bucket[k] = bucket.get(k, 0) + int(v)
    return {
        "state": "healthy",
        "last_run_age_s": _age(now, hb.get("last_run_s") or last.get("at")),
        "window_runs": len(window),
        "latest": dict(last),
        "window_totals": totals,
        "reject_counts": refused,
        "by_node_window": by_node,
        "by_source_window": by_source,
    }


def _source_report(drain: Dict[str, Any], score: Dict[str, Any],
                   tag: Dict[str, Any]) -> Dict[str, Any]:
    sources: Dict[str, Dict[str, Any]] = {}
    for node, n in (drain.get("nodes") or {}).items():
        sources.setdefault(node, {})["detections_last_added"] = n.get("last_detection_added")
        sources[node]["detection_freshness_age_s"] = n.get("last_success_age_s")
        sources[node]["clips_named_window"] = (n.get("clips") or {}).get("named_window")
    for node, vals in (score.get("by_node_window") or {}).items():
        sources.setdefault(node, {})["score_window"] = vals
    for node, vals in (tag.get("by_node_window") or {}).items():
        sources.setdefault(node, {})["tag_window"] = vals
    return sources


def build_data_report(nodes: Sequence[str], statuses: Mapping[str, Mapping[str, Any]],
                      drain_hb: Optional[Mapping[str, Any]],
                      score_hb: Optional[Mapping[str, Any]],
                      tag_hb: Optional[Mapping[str, Any]],
                      durable_health: Optional[Mapping[str, Any]],
                      now: Optional[float] = None,
                      window_s: float = DEFAULT_DATA_WINDOW_S) -> Dict[str, Any]:
    now = time.time() if now is None else now
    gaps: List[str] = []
    node_reports: Dict[str, Any] = {}
    sensors = (drain_hb or {}).get("sensors") if isinstance(drain_hb, Mapping) else {}
    sensors = sensors if isinstance(sensors, Mapping) else {}
    for node in nodes:
        st = statuses.get(node)
        if st is None:
            gaps.append("%s: status unknown" % node)
        sensor = sensors.get(node) if isinstance(sensors.get(node), Mapping) else {}
        if not sensor:
            gaps.append("%s: drain heartbeat unknown" % node)
        parsed = parse_status(st or {})
        clock = _status_clock(st)
        anchor_age = _status_anchor_age(st)
        mic_state = _status_mic(st)
        if clock["state"] == "unknown":
            gaps.append("%s: clock state unknown" % node)
        if anchor_age["state"] == "unknown":
            gaps.append("%s: anchor age unknown" % node)
        if mic_state["state"] == "unknown":
            gaps.append("%s: mic state unknown" % node)
        node_reports[node] = {
            "firmware": None if st is None else st.get("fw"),
            "class": parsed.get("class") if st is not None else None,
            "clock": clock,
            "anchor_age_s": anchor_age,
            "mic_state": mic_state,
            "heartbeat": _drain_node_report(node, sensor, now, window_s) if sensor else {
                "state": "unknown", "last_success_age_s": None,
            },
        }
    if drain_hb is None:
        gaps.append("hear-drain heartbeat missing")
    drain = {"state": "healthy" if drain_hb else "unknown", "nodes": {
        n: _drain_node_report(n, s, now, window_s)
        for n, s in sensors.items() if isinstance(s, Mapping)
    }}
    score = _workflow_report(score_hb, now, window_s,
                             ("newly_scored", "scored", "refused", "unparseable", "silent"))
    tag = _workflow_report(tag_hb, now, window_s,
                           ("tagged", "refused", "already_tagged", "deferred", "unparseable"))
    if score.get("state") == "unknown":
        gaps.append("hear-score heartbeat missing or empty")
    if tag.get("state") == "unknown":
        gaps.append("hear-tag heartbeat missing or empty")
    durable = durable_health.get("durable_store") if isinstance(durable_health, Mapping) else None
    if durable is None and isinstance(durable_health, Mapping):
        durable = durable_health
    if not isinstance(durable, Mapping):
        durable = {"state": "unknown", "backend": None, "pending_records": None}
        gaps.append("durable heartbeat receiver health missing")
    else:
        durable = {
            "state": "healthy",
            "backend": durable.get("backend"),
            "enabled": durable.get("enabled"),
            "path": durable.get("path"),
            "pending_records": durable.get("pending_records"),
            "cache_failures": durable.get("cache_failures"),
            "last_cache_failure_at": durable.get("last_cache_failure_at"),
        }
    return {
        "schema": "dama-fleet-data-report/v1",
        "generated_at_s": now,
        "window_s": window_s,
        "nodes": node_reports,
        "sources": _source_report(drain, score, tag),
        "workflows": {"drain": drain, "score": score, "tag": tag},
        "ingest_reject_counts": {
            "score": score.get("reject_counts") or {},
            "tag": tag.get("reject_counts") or {},
        },
        "durable_store": durable,
        "coverage_gaps": sorted(set(gaps)),
    }


def format_data_report(report: Mapping[str, Any]) -> str:
    lines = []
    lines.append("node                 fw           class              clock      anchor   mic        hb_age   det+  clips(named/fetch/defer/lost) scene_gap")
    lines.append("-------------------- ------------ ------------------ ---------- -------- ---------- -------- ----- ----------------------------- ---------")
    for node in sorted((report.get("nodes") or {})):
        n = report["nodes"][node]
        hb = n.get("heartbeat") or {}
        clips = hb.get("clips") or {}
        scene = hb.get("scene") or {}
        clock = (n.get("clock") or {}).get("value")
        anchor = (n.get("anchor_age_s") or {}).get("value")
        mic = (n.get("mic_state") or {}).get("value")
        lines.append("%-20s %-12s %-18s %-10s %-8s %-10s %-8s %-5s %5s/%-5s/%-5s/%-5s %-9s" % (
            node,
            str(n.get("firmware") or "UNKNOWN")[:12],
            str(n.get("class") or "UNKNOWN")[:18],
            str(clock or "UNKNOWN")[:10],
            "UNKNOWN" if anchor is None else _fmt_age(float(anchor)),
            str(mic or "UNKNOWN")[:10],
            _fmt_age(hb.get("last_success_age_s")),
            str(hb.get("last_detection_added") if hb.get("last_detection_added") is not None else "?"),
            str(clips.get("named_window") if clips.get("named_window") is not None else "?"),
            str(clips.get("fetched_window") if clips.get("fetched_window") is not None else "?"),
            str(clips.get("deferred_window") if clips.get("deferred_window") is not None else "?"),
            str(clips.get("destroyed_window") if clips.get("destroyed_window") is not None else "?"),
            str((scene.get("unfetched_bytes_window")
                 if scene.get("unfetched_bytes_window") is not None else "UNKNOWN")),
        ))
    wf = report.get("workflows") or {}
    for name in ("score", "tag"):
        w = wf.get(name) or {}
        total = (w.get("latest") or {}).get("records_seen" if name == "score" else "index_keys")
        lines.append("%-8s %-8s age=%s window_runs=%s total=%s window=%s rejects=%s" % (
            name, w.get("state", "unknown"), _fmt_age(w.get("last_run_age_s")),
            w.get("window_runs"), "UNKNOWN" if total is None else total,
            json.dumps(w.get("window_totals") or {}, sort_keys=True),
            json.dumps(w.get("reject_counts") or {}, sort_keys=True)))
    durable = report.get("durable_store") or {}
    lines.append("durable backend=%s pending=%s state=%s" % (
        durable.get("backend") or "UNKNOWN",
        "UNKNOWN" if durable.get("pending_records") is None else durable.get("pending_records"),
        durable.get("state", "unknown")))
    gaps = report.get("coverage_gaps") or []
    lines.append("coverage gaps: %s" % ("none" if not gaps else "; ".join(gaps)))
    return "\n".join(lines)


def _load_status_dir(path: Optional[str], warnings: WarningSink) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not path:
        return out
    base = Path(os.path.expanduser(path))
    if not base.exists():
        warnings.add("status JSON directory not found: %s" % base)
        return out
    for p in sorted(base.glob("*.json")):
        d = _load_json_file(str(p), warnings)
        if d is not None:
            out[_norm_name(str(d.get("node") or p.stem))] = d
    return out


def run_data_report(args: argparse.Namespace, warnings: WarningSink) -> int:
    nodes = parse_nodes(args)
    statuses = _load_status_dir(args.status_json_dir, warnings)
    if not args.no_status_probe:
        for spec in (args.node or []) + (args.nodes or []):
            name, url = split_target(spec)
            if name in statuses:
                continue
            res = fetch_status(url, args.timeout, args.retries, args.retry_backoff)
            if res.data:
                statuses[name] = res.data
    pool = os.path.expanduser(args.pool)
    drain_hb = _load_json_file(args.drain_heartbeat, warnings)
    score_hb = _load_json_file(args.score_heartbeat, warnings)
    tag_hb = _load_json_file(args.tag_heartbeat, warnings)
    durable = _load_json_file(args.durable_health, warnings)
    if args.from_kubectl:
        pod = find_pool_reader_pod(args.namespace, warnings, args.timeout)
        if pod:
            remote_pool = args.pool if str(args.pool).startswith("/") else "/pool/corpus"
            drain_hb = drain_hb or _kubectl_exec_json(
                args.namespace, pod, str(Path(remote_pool) / POOL_HEARTBEATS["drain"]),
                warnings, args.timeout)
            score_hb = score_hb or _kubectl_exec_json(args.namespace, pod,
                                                      str(Path(remote_pool)
                                                          / POOL_HEARTBEATS["score"]),
                                                      warnings, args.timeout)
            tag_hb = tag_hb or _kubectl_exec_json(args.namespace, pod,
                                                  str(Path(remote_pool) / POOL_HEARTBEATS["tag"]),
                                                  warnings, args.timeout)
        durable = durable or _kubectl_http_json(args.namespace, "deploy/hear-heartbeat",
                                                "http://127.0.0.1:5051/healthz",
                                                warnings, args.timeout)
    else:
        drain_hb = drain_hb or _read_pool_heartbeat(pool, POOL_HEARTBEATS["drain"], warnings)
        score_hb = score_hb or _read_pool_heartbeat(pool, POOL_HEARTBEATS["score"], warnings)
        tag_hb = tag_hb or _read_pool_heartbeat(pool, POOL_HEARTBEATS["tag"], warnings)

    report = build_data_report(nodes, statuses, drain_hb, score_hb, tag_hb, durable,
                               window_s=args.window_s)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_data_report(report))
    if warnings.items:
        print("\nwarnings:", file=sys.stderr)
        for item in warnings.items:
            print("- " + item, file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# CLI/report


def parse_nodes(args: argparse.Namespace) -> List[str]:
    values: List[str] = []
    for raw in (args.node or []) + (args.nodes or []):
        name, _ = split_target(raw)
        if name and name not in values:
            values.append(name)
    return values or list(EXPECTED_NODES)


def print_table(results: Sequence[NodeResult]) -> None:
    print("%-20s %-15s %-8s %-6s %-12s %-8s %-8s %-10s %-5s %-6s %-6s %-7s %s" % (
        "node", "ip", "net", "http", "gps", "ingest", "uptime", "pps", "utc", "rssi", "dets", "temp", "details"))
    print("%-20s %-15s %-8s %-6s %-12s %-8s %-8s %-10s %-5s %-6s %-6s %-7s %s" % (
        "-" * 20, "-" * 15, "-" * 8, "-" * 6, "-" * 7, "-" * 8, "-" * 8,
        "-" * 10, "-" * 5, "-" * 6, "-" * 6, "-" * 7, "-" * 7))
    for r in results:
        print("%-20s %-15s %-8s %-6s %-12s %-8s %-8s %-10s %-5s %-6s %-6s %-7s %s" % (
            r.node, r.ip, r.network, r.http, r.gps, r.ingestion, r.uptime, r.pps, r.utc,
            r.rssi, r.dets, r.temp, "; ".join(r.details)))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("nodes", nargs="*", help="optional node names; defaults to the expected fleet")
    ap.add_argument("--node", action="append", default=[], help="node to check; repeatable")
    ap.add_argument("--data-report", action="store_true",
                    help="summarize fleet DATA state from bounded live/fixture sources instead "
                         "of running the legacy three-layer online check")
    ap.add_argument("--json", action="store_true",
                    help="with --data-report, emit machine-readable JSON instead of the human table")
    ap.add_argument("--pool", default=DEFAULT_POOL,
                    help="pool root for local heartbeat files read by --data-report")
    ap.add_argument("--window-s", type=float, default=DEFAULT_DATA_WINDOW_S,
                    help="window for --data-report heartbeat ring totals")
    ap.add_argument("--status-json-dir",
                    help="fixture/source directory of per-node /status JSON files for --data-report")
    ap.add_argument("--no-status-probe", action="store_true",
                    help="with --data-report, do not probe node /status endpoints")
    ap.add_argument("--drain-heartbeat",
                    help="explicit hear-drain heartbeat.json for --data-report fixtures")
    ap.add_argument("--score-heartbeat",
                    help="explicit score_heartbeat.json for --data-report fixtures")
    ap.add_argument("--tag-heartbeat",
                    help="explicit tag_heartbeat.json for --data-report fixtures")
    ap.add_argument("--durable-health",
                    help="explicit hear-heartbeat /healthz JSON or durable_store object")
    ap.add_argument("--from-kubectl", action="store_true",
                    help="with --data-report, read bounded heartbeat JSON files from existing "
                         "cluster pods (head -c 1MiB only; no PVC scans and no cluster writes)")
    ap.add_argument("--opnsense", default=DEFAULT_OPNSENSE, help="OPNsense base URL")
    ap.add_argument("--dhcp-endpoint", default=DEFAULT_DHCP_ENDPOINT,
                    help="OPNsense DHCP leases endpoint, relative or absolute")
    ap.add_argument("--verify-opnsense-tls", action="store_true",
                    help="verify OPNsense TLS certificate (default skips verification for the LAN IP)")
    ap.add_argument("--namespace", default=DEFAULT_NAMESPACE,
                    help="Kubernetes namespace for the OPNsense secret and hear-drain pods")
    ap.add_argument("--secret", default=DEFAULT_SECRET, help="Kubernetes Secret with OPNsense creds")
    ap.add_argument("--survey", default=str(_repo_default_survey()), help="survey.json fallback path")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="HTTP/kubectl timeout seconds")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="/status retries per node")
    ap.add_argument("--retry-backoff", type=float, default=DEFAULT_BACKOFF_S, help="seconds between retries")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="kubectl logs --since window")
    ap.add_argument("--log-pods", type=int, default=8, help="recent hear-drain pods to inspect")
    ap.add_argument("--require-online", action="store_true", help="require all nodes to be online")
    ap.add_argument("--leases", help="JSON leases file for testing")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    warnings = WarningSink()
    if args.data_report:
        return run_data_report(args, warnings)

    nodes = parse_nodes(args)
    dhcp: Dict[str, str] = {}

    fallback = load_fallback_ip_map(Path(args.survey), warnings)
    if args.leases:
        try:
            payload = json.loads(Path(args.leases).read_text())
            leases = parse_opnsense_leases(payload, warnings)
            dhcp = dhcp_ip_map(nodes, leases, warnings)
        except Exception as exc:
            warnings.add("could not read leases file %s: %s" % (args.leases, exc))
    else:
        creds = get_credentials(args.namespace, args.secret, warnings)
        if creds:
            try:
                leases = query_opnsense_leases(args.opnsense, args.dhcp_endpoint, creds,
                                               args.timeout, args.verify_opnsense_tls, warnings)
                dhcp = dhcp_ip_map(nodes, leases, warnings)
            except Exception as exc:
                warnings.add(str(exc))

    resolved = resolve_node_ips(nodes, dhcp, fallback)
    results: Dict[str, NodeResult] = {}
    for spec in (args.node or []) + (args.nodes or []):
        if "=" in spec or spec.startswith("http://") or spec.startswith("https://"):
            name, url = split_target(spec, dhcp)
            ip_val = spec.split("=", 1)[1].strip() if "=" in spec else url
            resolved[name] = (ip_val, "TARGET")

    for node in nodes:
        ip, source = resolved.get(node, ("-", "FAIL"))
        r = NodeResult(node=node, ip=ip, network=source)
        results[node] = r
        if ip == "-":
            r.details.append("no DHCP lease or survey/default IP")
            continue
        spec_url = None
        for spec in (args.node or []) + (args.nodes or []):
            spec_name, spec_u = split_target(spec, dhcp)
            if spec_name == node:
                spec_url = spec_u
                break
        target_str = spec_url or ip
        try:
            res = fetch_status(target_str, args.timeout, args.retries, args.retry_backoff)
            if isinstance(res, dict):
                http = HttpResult(200, res, None)
            else:
                http = res
        except TypeError:
            try:
                res = fetch_status(target_str)
                if isinstance(res, dict):
                    http = HttpResult(200, res, None)
                else:
                    http = res
            except Exception as exc:
                http = HttpResult(None, None, str(exc))
        except Exception as exc:
            http = HttpResult(None, None, str(exc))
        apply_status_fields(r, http)
        if http.code == 200 and http.data is not None:
            r.http = "200"
            r.http_ok = True

        eval_res = evaluate_health(node, http.data if http.code == 200 else None, Exception(http.error) if http.error else None)
        r.state = eval_res["state"]
        if eval_res["state"] == "online":
            r.details.append("ONLINE")
        elif eval_res["state"] == "degraded":
            r.details.append("DEGRADED: %s" % ", ".join(eval_res["reasons"]))
        else:
            r.details.append("OFFLINE: %s" % ", ".join(eval_res["reasons"]))

    pods = find_hear_drain_pods(args.namespace, warnings, args.log_pods, args.timeout)
    logs = read_hear_drain_logs(args.namespace, args.since, pods, warnings, max(args.timeout, 30.0)) if pods else ""
    apply_ingestion(results, logs, warnings)

    ordered = [results[node] for node in nodes]
    print_table(ordered)
    ok_nodes = sum(1 for r in ordered if r.ok())
    print("\nsummary: %d/%d nodes have IP, HTTP 200 JSON status, and ingestion OK" % (ok_nodes, len(ordered)))
    if warnings.items:
        print("\nwarnings:", file=sys.stderr)
        for item in warnings.items:
            print("- " + item, file=sys.stderr)

    if args.require_online:
        return 0 if all(r.state == "online" for r in ordered) else 2
    return 0 if ok_nodes == len(ordered) else 0


if __name__ == "__main__":
    raise SystemExit(main())
