"""Strict, isolated receiver and bounded SQLite store for unvalidated BMA bearings."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional


SCHEMA = "bma.bearing.v1"
TELEMETRY_PATH = "bma/bearing"
DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024
DEFAULT_MAX_RECORDS = 10_000
DEFAULT_MAX_STORAGE_BYTES = 64 * 1024 * 1024
DEFAULT_RETENTION_S = 30 * 24 * 60 * 60
_UNIT = re.compile(r"[a-z][a-z0-9_-]{0,23}\Z", re.ASCII)
_ORIGIN = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z", re.ASCII)
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z", re.ASCII)
_AWS_IDEMPOTENCY = re.compile(r"[A-Za-z0-9._:-]{8,200}\Z", re.ASCII)
_AWS_TRACE_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z", re.ASCII)
_AWS_METADATA_KEYS = {"idempotency_key", "batch_idempotency_key", "ingest_trace_id", "ts"}
_MESSAGE_KEYS = {"device_id", "ts_ms", "telemetry_path", "schema", "observation"}
_OBSERVATION_KEYS = {
    "unit_id", "det_id", "source_time_wall_s", "origin_id", "pose",
    "bearing_deg_true", "sigma_deg", "clock_precise", "evidence",
}


class BmaPeerError(ValueError):
    """Base class for rejected configuration or BMA messages."""


class BmaValidationError(BmaPeerError):
    """A message does not satisfy the explicit BMA peer contract."""


class BmaStorageFull(BmaPeerError):
    """The configured finite row or byte budget is exhausted."""


class BmaIdentityConflict(BmaPeerError):
    """A replay reused an observation identity with different content."""


class BmaReceiverError(RuntimeError):
    """The optional MQTT receiver cannot be configured or started."""


def _finite_number(value: Any, field: str, *, minimum: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BmaValidationError("%s must be a finite number" % field)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (minimum is not None and value < minimum):
        raise BmaValidationError("%s is outside its permitted range" % field)
    return float(value)


def _object(value: Any, field: str, keys: set[str]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise BmaValidationError("%s must be an object" % field)
    if set(value) != keys:
        raise BmaValidationError("%s has missing or unexpected fields" % field)
    return value


def _pairs_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BmaValidationError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _reject_json_constant(value):
    raise BmaValidationError("nonfinite JSON value: %s" % value)


def normalize_device_id(value: str) -> str:
    """Match Oxalis `_topic_for`: spaces and underscores become hyphens, then lowercase."""
    if not isinstance(value, str):
        raise BmaValidationError("device_id must be a string")
    normalized = value.replace(" ", "-").replace("_", "-").lower()
    if not normalized or len(normalized) > 128:
        raise BmaValidationError("device_id cannot be normalized to a topic segment")
    return normalized


def _stable_sender_content(message: Dict[str, Any]) -> Dict[str, Any]:
    """Exclude AWS delivery metadata and its derived timestamp from replay identity."""
    ignored = _AWS_METADATA_KEYS
    return {key: value for key, value in message.items() if key not in ignored}


def _validate_observation(observation: Any) -> Dict[str, Any]:
    item = _object(observation, "observation", _OBSERVATION_KEYS)
    unit_id = item["unit_id"]
    det_id = item["det_id"]
    if not isinstance(unit_id, str) or _UNIT.fullmatch(unit_id) is None:
        raise BmaValidationError("observation.unit_id is invalid")
    if (not isinstance(det_id, str)
            or re.fullmatch(re.escape(unit_id) + r":[0-9]+:[0-9]+", det_id, re.ASCII) is None):
        raise BmaValidationError("observation.det_id does not match unit_id")
    origin_id = item["origin_id"]
    if not isinstance(origin_id, str) or _ORIGIN.fullmatch(origin_id) is None:
        raise BmaValidationError("observation.origin_id is invalid")
    _finite_number(item["source_time_wall_s"], "observation.source_time_wall_s", minimum=0)
    bearing = _finite_number(item["bearing_deg_true"], "observation.bearing_deg_true", minimum=0)
    if bearing >= 360:
        raise BmaValidationError("observation.bearing_deg_true must be below 360")
    if _finite_number(item["sigma_deg"], "observation.sigma_deg", minimum=0) == 0:
        raise BmaValidationError("observation.sigma_deg must be positive")
    if item["clock_precise"] is not False:
        raise BmaValidationError("observation.clock_precise must remain false")
    if item["evidence"] != "unvalidated":
        raise BmaValidationError("observation.evidence must remain unvalidated")

    pose_keys = {"origin_id", "position", "orientation", "source", "surveyed_utc"}
    pose = _object(item["pose"], "observation.pose", pose_keys)
    if pose["origin_id"] != origin_id:
        raise BmaValidationError("pose origin_id conflicts with observation origin_id")
    for key in ("source", "surveyed_utc"):
        if not isinstance(pose[key], str) or not pose[key].strip():
            raise BmaValidationError("observation.pose.%s must be nonempty text" % key)
        try:
            pose[key].encode("utf-8")
        except UnicodeEncodeError as exc:
            raise BmaValidationError(
                "observation.pose.%s must be valid UTF-8 text" % key) from exc
    position = _object(
        pose["position"], "observation.pose.position", {"e_m", "n_m", "u_m", "sigma_m"})
    for key in ("e_m", "n_m", "u_m"):
        _finite_number(position[key], "observation.pose.position." + key)
    if _finite_number(position["sigma_m"], "observation.pose.position.sigma_m", minimum=0) == 0:
        raise BmaValidationError("observation.pose.position.sigma_m must be positive")
    orientation = _object(
        pose["orientation"], "observation.pose.orientation",
        {"heading_deg", "pitch_deg", "roll_deg", "port_normal", "sigma_heading_deg"})
    heading = _finite_number(orientation["heading_deg"], "pose.orientation.heading_deg", minimum=0)
    pitch = _finite_number(orientation["pitch_deg"], "pose.orientation.pitch_deg")
    roll = _finite_number(orientation["roll_deg"], "pose.orientation.roll_deg")
    if not 0 <= heading < 360 or not -90 <= pitch <= 90 or not -180 < roll <= 180:
        raise BmaValidationError("observation.pose.orientation angle is outside its permitted range")
    if orientation["port_normal"] not in ("+z", "-z"):
        raise BmaValidationError("observation.pose.orientation.port_normal is invalid")
    if _finite_number(
            orientation["sigma_heading_deg"], "pose.orientation.sigma_heading_deg",
            minimum=0) == 0:
        raise BmaValidationError("observation.pose.orientation.sigma_heading_deg must be positive")
    return item


def decode_message(raw: bytes, topic: str, *, expected_site_id: Optional[str] = None,
                   max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> Dict[str, Any]:
    """Decode and validate one routed MQTT record before it reaches durable storage."""
    if not isinstance(raw, (bytes, bytearray)):
        raise BmaValidationError("MQTT payload must be bytes")
    if len(raw) > max_message_bytes:
        raise BmaValidationError("MQTT payload exceeds the configured byte limit")
    try:
        message = json.loads(
            bytes(raw).decode("utf-8"),
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except BmaValidationError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BmaValidationError("MQTT payload is not bounded UTF-8 JSON") from exc
    if not isinstance(message, dict):
        raise BmaValidationError("MQTT payload must be a JSON object")
    allowed = _MESSAGE_KEYS | {"site_id"} | _AWS_METADATA_KEYS
    if set(message) - allowed or _MESSAGE_KEYS - set(message):
        raise BmaValidationError("message has missing or unexpected fields")
    if message["schema"] != SCHEMA:
        raise BmaValidationError("unsupported BMA schema")
    if message["telemetry_path"] != TELEMETRY_PATH:
        raise BmaValidationError("unsupported BMA telemetry_path")
    observation = _validate_observation(message["observation"])
    expected_device_id = "bma-" + observation["unit_id"]
    body_device_id = message["device_id"]
    if body_device_id != expected_device_id:
        raise BmaValidationError("device_id conflicts with observation.unit_id")
    ts_ms = message["ts_ms"]
    if isinstance(ts_ms, bool) or not isinstance(ts_ms, int) or ts_ms < 0:
        raise BmaValidationError("ts_ms must be a nonnegative integer")
    source_seconds = _finite_number(
        observation["source_time_wall_s"], "observation.source_time_wall_s", minimum=0)
    if source_seconds > (2**63 - 1) / 1000 or ts_ms > 2**63 - 1:
        raise BmaValidationError("display timestamp exceeds SQLite integer range")
    source_ms = int(source_seconds * 1000)
    if ts_ms != source_ms:
        raise BmaValidationError("ts_ms conflicts with display-only source_time_wall_s")
    if "ts" in message:
        normalized_ts = message["ts"]
        if (isinstance(normalized_ts, bool) or not isinstance(normalized_ts, int)
                or normalized_ts != ts_ms):
            raise BmaValidationError("AWS-normalized ts conflicts with ts_ms")
    for key in ("idempotency_key", "batch_idempotency_key"):
        if key in message and (
                not isinstance(message[key], str)
                or _AWS_IDEMPOTENCY.fullmatch(message[key]) is None):
            raise BmaValidationError("%s is invalid AWS idempotency metadata" % key)
    if "ingest_trace_id" in message and (
            not isinstance(message["ingest_trace_id"], str)
            or _AWS_TRACE_ID.fullmatch(message["ingest_trace_id"]) is None):
        raise BmaValidationError("ingest_trace_id is invalid AWS trace metadata")

    if "site_id" in message:
        site_id = message["site_id"]
        if not isinstance(site_id, str) or _SITE.fullmatch(site_id) is None:
            raise BmaValidationError("site_id is invalid")
    else:
        site_id = None
    if expected_site_id is not None:
        if not isinstance(expected_site_id, str) or _SITE.fullmatch(expected_site_id) is None:
            raise BmaValidationError("configured site_id is invalid")
        if site_id != expected_site_id:
            raise BmaValidationError("message site_id does not match configured site_id")

    parts = topic.split("/") if isinstance(topic, str) else []
    if len(parts) != 3 or parts[0] != "dama" or parts[2] != "telemetry":
        raise BmaValidationError("topic must be dama/<normalized-device-id>/telemetry")
    topic_device_id = parts[1]
    normalized_topic_id = normalize_device_id(topic_device_id)
    if topic_device_id != normalized_topic_id:
        raise BmaValidationError("topic device_id is not normalized")
    if normalized_topic_id != normalize_device_id(body_device_id):
        raise BmaValidationError("topic device_id conflicts with message device_id")
    return message


class BmaPeerStore:
    """A finite, replay-safe SQLite ledger; a successful insert is durably committed."""

    def __init__(self, path, *, max_records: int = DEFAULT_MAX_RECORDS,
                 max_storage_bytes: int = DEFAULT_MAX_STORAGE_BYTES,
                 retention_s: int = DEFAULT_RETENTION_S,
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES):
        for label, value in (("max_records", max_records),
                             ("max_storage_bytes", max_storage_bytes),
                             ("retention_s", retention_s),
                             ("max_message_bytes", max_message_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise BmaPeerError("%s must be a positive integer" % label)
        if max_message_bytes > max_storage_bytes:
            raise BmaPeerError("max_message_bytes cannot exceed max_storage_bytes")
        self.path = str(path)
        if not self.path.strip() or self.path == ":memory:" or self.path.startswith("file:"):
            raise BmaPeerError("BMA peer storage requires a durable SQLite file")
        self.max_records = max_records
        self.max_storage_bytes = max_storage_bytes
        self.retention_s = retention_s
        self.max_message_bytes = max_message_bytes
        try:
            self._initialize()
        except sqlite3.DatabaseError as exc:
            raise BmaPeerError("cannot initialize BMA SQLite store: %s" % exc) from exc

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
        except Exception:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS bma_observations (
                    origin_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    det_id TEXT NOT NULL,
                    site_id TEXT,
                    ts_ms INTEGER NOT NULL,
                    received_at_ms INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    stable_hash TEXT,
                    byte_count INTEGER NOT NULL,
                    PRIMARY KEY (origin_id, device_id, det_id)
                )
            """)
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(bma_observations)").fetchall()}
            if "stable_hash" not in columns:
                connection.execute("ALTER TABLE bma_observations ADD COLUMN stable_hash TEXT")
            for rowid, stored_json in connection.execute(
                    "SELECT rowid, record_json FROM bma_observations "
                    "WHERE stable_hash IS NULL").fetchall():
                stable_content = json.dumps(
                    _stable_sender_content(json.loads(stored_json)), sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
                stable_hash = hashlib.sha256(stable_content).hexdigest()
                record_bytes = len(stored_json.encode("utf-8"))
                connection.execute(
                    "UPDATE bma_observations SET stable_hash=?, byte_count=? WHERE rowid=?",
                    (stable_hash, record_bytes + len(stable_hash), rowid))
            connection.execute(
                "CREATE INDEX IF NOT EXISTS bma_observations_received "
                "ON bma_observations(received_at_ms)")
            cutoff = time.time_ns() // 1_000_000 - self.retention_s * 1000
            connection.execute(
                "DELETE FROM bma_observations WHERE received_at_ms < ?", (cutoff,))
            count, used = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(byte_count), 0) FROM bma_observations"
            ).fetchone()
            if count > self.max_records or used > self.max_storage_bytes:
                raise BmaStorageFull(
                    "existing BMA store exceeds configured record or byte limits")

    def accept(self, raw: bytes, topic: str, *, expected_site_id: Optional[str] = None,
               now_ms: Optional[int] = None) -> Dict[str, Any]:
        if now_ms is None:
            now_ms = time.time_ns() // 1_000_000
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise BmaPeerError("now_ms must be a nonnegative integer")
        record = decode_message(
            raw, topic, expected_site_id=expected_site_id,
            max_message_bytes=self.max_message_bytes)
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
        stable_canonical = json.dumps(
            _stable_sender_content(record), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode("utf-8")
        stable_hash = hashlib.sha256(stable_canonical).hexdigest()
        encoded_size = len(canonical.encode("utf-8"))
        stored_size = encoded_size + len(stable_hash)
        if encoded_size > self.max_message_bytes or stored_size > self.max_storage_bytes:
            raise BmaStorageFull("validated record exceeds the configured byte limit")
        observation = record["observation"]
        origin_id = observation["origin_id"]
        device_id = record["device_id"]
        det_id = observation["det_id"]
        site_id = record.get("site_id")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cutoff = now_ms - self.retention_s * 1000
                connection.execute(
                    "DELETE FROM bma_observations WHERE received_at_ms < ?", (cutoff,))
                existing = connection.execute(
                    "SELECT stable_hash FROM bma_observations "
                    "WHERE origin_id=? AND device_id=? AND det_id=?",
                    (origin_id, device_id, det_id)).fetchone()
                if existing is not None:
                    if existing[0] == stable_hash:
                        return {"status": "duplicate", "origin_id": origin_id,
                                "device_id": device_id, "det_id": det_id}
                    raise BmaIdentityConflict(
                        "origin_id/device_id/det_id already exists with different content")
                count, used = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(byte_count), 0) FROM bma_observations"
                ).fetchone()
                if count >= self.max_records or used + stored_size > self.max_storage_bytes:
                    raise BmaStorageFull("configured BMA observation capacity is full")
                connection.execute(
                    "INSERT INTO bma_observations "
                    "(origin_id, device_id, det_id, site_id, ts_ms, received_at_ms, "
                    "record_json, stable_hash, byte_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (origin_id, device_id, det_id, site_id, record["ts_ms"], now_ms,
                     canonical, stable_hash, stored_size))
                connection.commit()
        except sqlite3.DatabaseError as exc:
            raise BmaPeerError("SQLite operation failed before durable acknowledgement: %s" % exc) from exc
        return {"status": "stored", "origin_id": origin_id,
                "device_id": device_id, "det_id": det_id}

    def _prune(self, connection, now_ms: int) -> None:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM bma_observations WHERE received_at_ms < ?",
            (now_ms - self.retention_s * 1000,))
        connection.commit()

    def list_observations(self, *, limit: int = 100,
                          now_ms: Optional[int] = None) -> list[Dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise BmaPeerError("limit must be an integer from 1 to 1000")
        if now_ms is None:
            now_ms = time.time_ns() // 1_000_000
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise BmaPeerError("now_ms must be a nonnegative integer")
        with self._connection() as connection:
            self._prune(connection, now_ms)
            rows = connection.execute(
                "SELECT record_json, received_at_ms FROM bma_observations "
                "ORDER BY received_at_ms DESC, origin_id, device_id, det_id LIMIT ?",
                (limit,)).fetchall()
        return [{"received_at_ms": received_at_ms, **json.loads(record_json)}
                for record_json, received_at_ms in rows]

    def counts(self, *, now_ms: Optional[int] = None) -> Dict[str, int]:
        if now_ms is None:
            now_ms = time.time_ns() // 1_000_000
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise BmaPeerError("now_ms must be a nonnegative integer")
        with self._connection() as connection:
            self._prune(connection, now_ms)
            count, used = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(byte_count), 0) FROM bma_observations"
            ).fetchone()
        return {"records": count, "stored_bytes": used}
