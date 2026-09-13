"""Shared deployment-readiness checks for hear-node enrollment and rollout gates."""
from __future__ import annotations

from datetime import datetime
import os
import time


SELFTEST_ORDER = ("mic", "gps", "pps", "wifi")
TIMESTAMP_PATHS = (
    ("captured_at",),
    ("validated_at",),
    ("recorded_at",),
    ("timestamp",),
    ("status", "captured_at"),
    ("status", "validated_at"),
    ("status", "timestamp"),
)


def _dig(record, path):
    cur = record
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def extract_selftest(record):
    """Return the structured self-test block from a status/evidence record, if any."""
    if isinstance(record.get("selftest"), dict):
        return record["selftest"]
    status = record.get("status")
    if isinstance(status, dict) and isinstance(status.get("selftest"), dict):
        return status["selftest"]
    return None


def format_selftest(record) -> str:
    st = extract_selftest(record) or {}
    return " ".join("%s=%s" % (k, st.get(k, "missing")) for k in SELFTEST_ORDER)


def selftest_reasons(record, allow_gps_no_fix_indoors=False, allow_pps_absent=False):
    """Return reasons why a node's self-test is not healthy enough to declare ready."""
    st = extract_selftest(record)
    if not isinstance(st, dict):
        return ["status has no selftest block"]
    reasons = []
    mic = st.get("mic")
    if mic != "ok":
        reasons.append("selftest mic=%r (need 'ok')" % mic)
    gps = st.get("gps")
    if gps == "ok":
        pass
    elif gps == "no-fix" and allow_gps_no_fix_indoors:
        pass
    else:
        reasons.append("selftest gps=%r (need 'ok'%s)" % (
            gps, " or explicit indoor no-fix acceptance" if gps == "no-fix" else ""))
    pps = st.get("pps")
    if pps == "ok":
        pass
    elif pps == "absent" and allow_pps_absent:
        pass
    else:
        reasons.append("selftest pps=%r (need 'ok'%s)" % (
            pps, " or explicit acceptance" if pps == "absent" else ""))
    wifi = st.get("wifi")
    if wifi != "ok":
        reasons.append("selftest wifi=%r (need 'ok')" % wifi)
    return reasons


def status_reasons(status, node=None, require_nvs=True, allow_gps_no_fix_indoors=False,
                   allow_pps_absent=False):
    reasons = []
    if node and status.get("node") != node:
        reasons.append("status reports node=%r, not %r" % (status.get("node"), node))
    prov = status.get("prov")
    if require_nvs:
        if not isinstance(prov, dict):
            reasons.append("status has no prov block")
        elif prov.get("src") != "nvs":
            reasons.append("status prov.src=%r, not 'nvs'" % prov.get("src"))
    reasons.extend(selftest_reasons(
        status,
        allow_gps_no_fix_indoors=allow_gps_no_fix_indoors,
        allow_pps_absent=allow_pps_absent,
    ))
    return reasons


def _parse_timestamp(value):
    if value is None:
        raise ValueError("missing timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return float(text)


def evidence_timestamp_s(evidence, evidence_path):
    """Return (epoch_seconds, source_description) from JSON metadata or file mtime."""
    for path in TIMESTAMP_PATHS:
        value = _dig(evidence, path)
        if value is None:
            continue
        try:
            return _parse_timestamp(value), ".".join(path)
        except (TypeError, ValueError):
            continue
    return os.path.getmtime(evidence_path), "file-mtime"


def evidence_age_reason(evidence, evidence_path, stale_max_age_s=900, now=None):
    """Why an evidence file is too stale to trust for a ready/validated decision, or None."""
    if stale_max_age_s is None:
        return None
    now_s = time.time() if now is None else float(now)
    seen_s, source = evidence_timestamp_s(evidence, evidence_path)
    age_s = max(0.0, now_s - seen_s)
    if age_s > float(stale_max_age_s):
        return ("evidence is stale: %.1f s old from %s (max %.1f s)"
                % (age_s, source, float(stale_max_age_s)))
    return None
