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
PMTK_STATUS_CLASSES: Tuple[str, ...] = (
    "esp32s3-i2s-gps",
    "esp32s3-speaker",
    "puc-pps",
    "puc-ntp",
)

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
    rssi = _first(d, (("net", "rssi"), ("wifi", "rssi"), ("rssi",)))
    sd = _first(d, (("sd",),))
    sd_free_mb = _first(d, (("sd_free_mb",),))
    return {
        "class": node_class,
        "fix": fix,
        "sats": sats,
        "tacc_ns": tacc_ns,
        "pps_edges": edges,
        "pps_spread_us": spread_us,
        "pps_glitches": glitches,
        "time_valid": utc,
        "rssi": rssi,
        "sd": sd,
        "sd_free_mb": sd_free_mb,
    }


def _gps_fix_ok(p: Mapping[str, Any]) -> bool:
    # `gps.fix` is protocol-specific: PMTK boards report NMEA GGA fix quality (1 = a live fix)
    # while UBX boards report u-blox fixType (3 = 3D). One shared `< 3` rule marks every healthy
    # PMTK node degraded, which is exactly the bug PR #135 fixed in the firmware self-test.
    fix = p.get("fix")
    if fix is None:
        return False
    try:
        n = int(fix)
    except (TypeError, ValueError):
        return False
    node_class = _norm_name(str(p.get("class") or ""))
    return n >= 1 if node_class in PMTK_STATUS_CLASSES else n >= 3


def evaluate_health(target_name: str, status_data: Optional[Mapping[str, Any]],
                    err: Optional[Exception] = None) -> Dict[str, Any]:
    if status_data is None:
        reason = str(err) if err else "unreachable"
        return {"state": "offline", "reasons": [reason], "node": target_name}

    reasons: List[str] = []
    p = parse_status(status_data)

    if not _gps_fix_ok(p):
        reasons.append("fix=%s" % p["fix"])
    if p["time_valid"] is not True:
        reasons.append("no UTC anchor")
    if p["pps_edges"] is None or p["pps_edges"] == 0:
        reasons.append("timebase never locked")
    if p["pps_glitches"] and p["pps_glitches"] > 0:
        reasons.append("%d pps glitch(es)" % p["pps_glitches"])
    if p["rssi"] is not None and p["rssi"] < -80:
        reasons.append("rssi=%d dBm" % p["rssi"])
    if p["sd"] is False:
        reasons.append("no SD card")
    elif p["sd_free_mb"] is not None and p["sd_free_mb"] < 100:
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

    fix = _first(d, (("gps", "fix"), ("gps_fix",), ("fix",)))
    sats = _first(d, (("gps", "sats"), ("gps", "satellites"), ("gps_sats",), ("sats",)))
    result.gps = "%s/%s" % (_json_scalar(fix), _json_scalar(sats))

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
# CLI/report


def parse_nodes(args: argparse.Namespace) -> List[str]:
    values: List[str] = []
    for raw in (args.node or []) + (args.nodes or []):
        name, _ = split_target(raw)
        if name and name not in values:
            values.append(name)
    return values or list(EXPECTED_NODES)


def print_table(results: Sequence[NodeResult]) -> None:
    print("%-20s %-15s %-8s %-6s %-7s %-8s %-8s %-10s %-5s %-6s %-6s %-7s %s" % (
        "node", "ip", "net", "http", "gps", "ingest", "uptime", "pps", "utc", "rssi", "dets", "temp", "details"))
    print("%-20s %-15s %-8s %-6s %-7s %-8s %-8s %-10s %-5s %-6s %-6s %-7s %s" % (
        "-" * 20, "-" * 15, "-" * 8, "-" * 6, "-" * 7, "-" * 8, "-" * 8,
        "-" * 10, "-" * 5, "-" * 6, "-" * 6, "-" * 7, "-" * 7))
    for r in results:
        print("%-20s %-15s %-8s %-6s %-7s %-8s %-8s %-10s %-5s %-6s %-6s %-7s %s" % (
            r.node, r.ip, r.network, r.http, r.gps, r.ingestion, r.uptime, r.pps, r.utc,
            r.rssi, r.dets, r.temp, "; ".join(r.details)))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("nodes", nargs="*", help="optional node names; defaults to the expected fleet")
    ap.add_argument("--node", action="append", default=[], help="node to check; repeatable")
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
