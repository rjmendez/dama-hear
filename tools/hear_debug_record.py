#!/usr/bin/env python3
"""Fetch a few explicitly authorized node clips, derive metrics, and retain no WAVs.

This is an operator-only diagnostic path. It does not change hear-drain and it never writes raw
audio into the pool. Each WAV exists only in a private temporary directory, is measured
immediately, and is unlinked before the next clip is fetched. The durable output is one JSON audit
document containing authorization metadata, bounds, derived measurements, and deletion proof.
"""
from __future__ import annotations

import argparse
import array
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from hear import clips as CL                                        # noqa: E402
from hear import identity as ID                                     # noqa: E402
from tools import hear_drain as HD                                  # noqa: E402

SCHEMA = "hear.debug_recording_audit.v1"
ACK = "I_ACKNOWLEDGE_RAW_AUDIO_IS_TEMPORARY"
MAX_CLIPS = 6
MAX_TTL_S = 300
MAX_WINDOW_S = 300
MAX_BYTE_CAP = 6 * 1024 * 1024
ALLOWLIST = {
    "nyquist": "172.16.100.105",
    "mach": "172.16.100.116",
    "rankine": "172.16.100.50",
    "gold": "172.16.100.82",
    "kasami": "172.16.100.90",
    "ageev": "172.16.100.83",
}
GRANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")


class DebugRecordingError(RuntimeError):
    pass


def parse_utc(value: str) -> dt.datetime:
    """Parse an explicitly UTC ISO-8601 timestamp; local and offset times are refused."""
    if not value.endswith("Z"):
        raise argparse.ArgumentTypeError("timestamp must end in Z (explicit UTC)")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as e:
        raise argparse.ArgumentTypeError("invalid UTC timestamp %r" % value) from e
    if parsed.utcoffset() != dt.timedelta(0):
        raise argparse.ArgumentTypeError("timestamp must be UTC")
    return parsed


def utc_text(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_text(name: str, value: str, minimum: int = 1) -> str:
    value = value.strip()
    if len(value) < minimum:
        raise DebugRecordingError("%s must be at least %d characters" % (name, minimum))
    if any(ord(c) < 32 for c in value):
        raise DebugRecordingError("%s contains control characters" % name)
    return value


def validate_request(args: argparse.Namespace) -> None:
    if not GRANT_RE.fullmatch(args.grant_id):
        raise DebugRecordingError("--grant-id must be 3-128 safe identifier characters")
    args.operator = _validate_text("--operator", args.operator, 2)
    args.reason = _validate_text("--reason", args.reason, 8)
    if args.ack != ACK:
        raise DebugRecordingError("--ack must exactly equal %r" % ACK)
    if args.window_end <= args.window_start:
        raise DebugRecordingError("--window-end must be after --window-start")
    window_s = (args.window_end - args.window_start).total_seconds()
    if window_s > MAX_WINDOW_S:
        raise DebugRecordingError("UTC window must be <= %d seconds" % MAX_WINDOW_S)
    if not 1 <= args.clip_count <= MAX_CLIPS:
        raise DebugRecordingError("--clip-count must be between 1 and %d" % MAX_CLIPS)
    if not 1 <= args.ttl_s <= MAX_TTL_S:
        raise DebugRecordingError("--ttl-s must be between 1 and %d" % MAX_TTL_S)
    if not 1 <= args.byte_cap <= MAX_BYTE_CAP:
        raise DebugRecordingError("--byte-cap must be between 1 and %d" % MAX_BYTE_CAP)
    if args.timeout <= 0 or args.timeout > args.ttl_s:
        raise DebugRecordingError("--timeout must be > 0 and <= --ttl-s")


def _remaining(deadline: float, timeout: float) -> float:
    left = deadline - time.time()
    if left <= 0:
        raise DebugRecordingError("TTL expired")
    return min(timeout, left)


def _pcm_metrics(body: bytes, probe: Dict[str, Any]) -> Dict[str, Any]:
    samples = array.array("h")
    samples.frombytes(body[44:])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        raise DebugRecordingError("WAV contains no PCM samples")
    square_sum = 0
    abs_sum = 0
    peak = 0
    crossings = 0
    previous = samples[0]
    for sample in samples:
        square_sum += sample * sample
        abs_sum += abs(sample)
        peak = max(peak, abs(sample))
        if (previous < 0 <= sample) or (previous >= 0 > sample):
            crossings += 1
        previous = sample
    rms = math.sqrt(square_sum / len(samples))

    def dbfs(value: float) -> Optional[float]:
        return None if value <= 0 else round(20.0 * math.log10(value / 32768.0), 3)

    return {
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "sample_count": len(samples),
        "fs_hz": probe["fs_hz"],
        "duration_s": round(float(probe["dur_s"]), 6),
        "rms_dbfs": dbfs(rms),
        "peak_dbfs": dbfs(float(peak)),
        "mean_abs": round(abs_sum / len(samples), 3),
        "zero_crossings": crossings,
    }


def extract_and_delete(workspace: str, body: bytes, basename: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Write one temporary WAV, derive non-audio metrics, then prove its immediate deletion."""
    path = os.path.join(workspace, basename)
    with open(path, "xb") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        with open(path, "rb") as fh:
            stored = fh.read()
        probe = CL.wav_probe(stored)
        if not probe["ok"]:
            raise DebugRecordingError("invalid WAV: %s" % probe["reason"])
        derived = _pcm_metrics(stored, probe)
    finally:
        if os.path.exists(path):
            os.unlink(path)
    proof = {
        "temporary_basename": basename,
        "raw_deleted": not os.path.exists(path),
    }
    if not proof["raw_deleted"]:
        raise DebugRecordingError("failed to delete temporary raw audio %s" % basename)
    return derived, proof


def _candidate_rows(node: str, ip: str, start_s: float, end_s: float,
                    timeout: float) -> List[Dict[str, Any]]:
    bodies = []
    for name in HD.DETS_FILES:
        body = HD.fetch_sd(ip, name, timeout)
        if body:
            bodies.append((name, body))
    candidates = [
        row for row in HD.clip_candidates(bodies, node)
        if row.get("anchored") and start_s <= float(row["ts_utc_s"]) <= end_s
    ]
    return candidates


def run(args: argparse.Namespace) -> Dict[str, Any]:
    validate_request(args)
    started = time.time()
    deadline = started + args.ttl_s
    node = args.node
    ip = ALLOWLIST[node]
    audit: Dict[str, Any] = {
        "schema": SCHEMA,
        "authorization": {
            "grant_id": args.grant_id,
            "operator": args.operator,
            "reason": args.reason,
            "ack": args.ack,
        },
        "request": {
            "node": node,
            "allowlisted_ip": ip,
            "window_start": utc_text(args.window_start),
            "window_end": utc_text(args.window_end),
            "clip_count_cap": args.clip_count,
            "ttl_s": args.ttl_s,
            "raw_byte_cap": args.byte_cap,
        },
        "started_utc": utc_text(dt.datetime.fromtimestamp(started, dt.timezone.utc)),
        "clips": [],
        "refusals": [],
    }
    workspace_path: Optional[str] = None
    cleanup_rows: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="hear-debug-recording-") as workspace:
        workspace_path = workspace
        os.chmod(workspace, 0o700)
        status = HD.fetch_status(ip, _remaining(deadline, args.timeout))
        reported = str(status.get("node") or "").strip()
        if reported != node and ID.alias_of(reported) != node:
            raise DebugRecordingError(
                "allowlisted target %s reported node identity %r" % (node, reported))
        candidates = _candidate_rows(
            node, ip, args.window_start.timestamp(), args.window_end.timestamp(),
            _remaining(deadline, args.timeout))
        audit["candidates_in_window"] = len(candidates)
        raw_bytes = 0
        for candidate in candidates[:args.clip_count]:
            if time.time() >= deadline:
                audit["refusals"].append({"clip": candidate["clip"], "reason": "ttl"})
                break
            body, reason = HD.fetch_clip(
                ip, candidate["clip"], _remaining(deadline, args.timeout),
                max_bytes=args.byte_cap - raw_bytes)
            if reason is not None:
                audit["refusals"].append({
                    "clip": candidate["clip"],
                    "reason": "raw_byte_cap" if reason == "byte_cap" else reason,
                })
                if reason == "byte_cap":
                    break
                continue
            raw_bytes += len(body)
            derived, proof = extract_and_delete(
                workspace, body, candidate["parts"]["basename"])
            cleanup_rows.append(proof)
            audit["clips"].append({
                "clip": candidate["clip"],
                "clip_key": candidate["clip_key"],
                "captured_utc": utc_text(dt.datetime.fromtimestamp(
                    float(candidate["ts_utc_s"]), dt.timezone.utc)),
                "derived": derived,
            })
        audit["raw_bytes_processed"] = raw_bytes
        remaining = sorted(os.listdir(workspace))
        if remaining:
            raise DebugRecordingError(
                "temporary workspace not empty after extraction: %r" % remaining)

    audit["completed_utc"] = utc_text(dt.datetime.now(dt.timezone.utc))
    audit["elapsed_s"] = round(time.time() - started, 6)
    audit["cleanup"] = {
        "storage_class": "private temporary workspace (Kubernetes emptyDir compatible)",
        "per_clip": cleanup_rows,
        "workspace_deleted": bool(workspace_path) and not os.path.exists(workspace_path),
        "raw_files_remaining": 0,
    }
    if not audit["cleanup"]["workspace_deleted"]:
        raise DebugRecordingError("temporary workspace still exists after cleanup")
    audit["result"] = "complete"
    return audit


def write_audit(path: str, audit: Dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(audit, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grant-id", required=True)
    ap.add_argument("--operator", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--ack", required=True, help="must exactly acknowledge temporary raw audio")
    ap.add_argument("--node", required=True, choices=sorted(ALLOWLIST))
    ap.add_argument("--window-start", required=True, type=parse_utc)
    ap.add_argument("--window-end", required=True, type=parse_utc)
    ap.add_argument("--clip-count", type=int, required=True)
    ap.add_argument("--ttl-s", type=int, required=True)
    ap.add_argument("--byte-cap", type=int, required=True)
    ap.add_argument("--output", required=True, help="durable JSON audit/derived-output path")
    ap.add_argument("--timeout", type=float, default=30.0)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    try:
        audit = run(args)
    except DebugRecordingError as e:
        ap.error(str(e))
    write_audit(args.output, audit)
    print(json.dumps({
        "result": audit["result"],
        "node": audit["request"]["node"],
        "clips": len(audit["clips"]),
        "raw_bytes_processed": audit["raw_bytes_processed"],
        "raw_files_remaining": audit["cleanup"]["raw_files_remaining"],
        "audit": os.path.abspath(args.output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
