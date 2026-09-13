#!/usr/bin/env python3
"""Tiny hear_node heartbeat/event receiver that writes short-TTL Redis state."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlparse

import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "100.73.200.19")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "30379"))
REDIS_PASS = os.environ.get("REDIS_PASS")
API_PORT = int(os.environ.get("HEAR_HEARTBEAT_PORT", "5051"))
HEARTBEAT_TTL_S = int(os.environ.get("HEAR_HEARTBEAT_TTL_S", "30"))
MAX_BODY_BYTES = int(os.environ.get("HEAR_HEARTBEAT_MAX_BODY_BYTES", "8192"))
EVENT_STREAM_KEY = os.environ.get("HEAR_EVENT_STREAM_KEY", "dama:hear:events")
EVENT_STREAM_MAXLEN = int(os.environ.get("HEAR_EVENT_STREAM_MAXLEN", "1024"))
RECEIVER_SCHEMA_VERSION = 1


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class HeartbeatReceiverStore:
    def __init__(self, client: Any, heartbeat_ttl_s: int = HEARTBEAT_TTL_S,
                 event_stream_key: str = EVENT_STREAM_KEY,
                 event_stream_maxlen: int = EVENT_STREAM_MAXLEN,
                 redis_target: Optional[str] = None):
        self.client = client
        self.heartbeat_ttl_s = heartbeat_ttl_s
        self.event_stream_key = event_stream_key
        self.event_stream_maxlen = event_stream_maxlen
        self.redis_target = redis_target or "unknown"

    def write_heartbeat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        record = dict(payload)
        record["received_at"] = utc_now()
        record["receiver_schema_version"] = RECEIVER_SCHEMA_VERSION
        body = encode_json(record)
        node_id = record["device_id"]
        pipe = self.client.pipeline(transaction=False)
        pipe.setex(f"dama:hear:{node_id}", self.heartbeat_ttl_s, body)
        pipe.sadd("dama:hear:devices", node_id)
        pipe.set("dama:hear:latest", body)
        pipe.execute()
        return record

    def write_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        record = dict(payload)
        record["received_at"] = utc_now()
        record["receiver_schema_version"] = RECEIVER_SCHEMA_VERSION
        body = encode_json(record)
        node_id = record["device_id"]
        pipe = self.client.pipeline(transaction=False)
        pipe.sadd("dama:hear:devices", node_id)
        pipe.set(f"dama:hear:event:{node_id}", body)
        pipe.xadd(self.event_stream_key, {"device_id": node_id, "payload": body},
                  maxlen=self.event_stream_maxlen, approximate=True)
        pipe.execute()
        return record


class ReceiverServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_cls, store: HeartbeatReceiverStore):
        super().__init__(server_address, handler_cls)
        self.store = store


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def _require_object(payload: Any, label: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise RequestError(f"{label} must be an object")
    return payload


def _require_string(payload: Mapping[str, Any], key: str, *, allow_null: bool = False) -> Optional[str]:
    value = payload.get(key)
    if value is None and allow_null:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RequestError(f"{key} must be a non-empty string")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(f"{key} must be an integer")
    return value


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise RequestError(f"{key} must be a boolean")
    return value


def _validate_common(payload: Any, telemetry_path: str) -> Dict[str, Any]:
    body = _require_object(payload, "payload")
    if body.get("telemetry_path") != telemetry_path:
        raise RequestError(f"telemetry_path must be {telemetry_path!r}")
    if body.get("telemetry_schema_version") != 1:
        raise RequestError("telemetry_schema_version must be 1")
    _require_string(body, "device_id")
    _require_string(body, "class")
    _require_string(body, "fw_version")
    _require_int(body, "uptime_s")
    if body.get("ts") is not None:
        _require_string(body, "ts")

    gps = _require_object(body.get("gps"), "gps")
    _require_int(gps, "fix")

    time_state = _require_object(body.get("time"), "time")
    _require_bool(time_state, "valid")
    return body


def validate_heartbeat_payload(payload: Any) -> Dict[str, Any]:
    body = _validate_common(payload, "hear/heartbeat")
    counters = _require_object(body.get("counters"), "counters")
    for key in ("scene_rows_written", "dets_rows_written", "clips_written", "clips_evicted"):
        _require_int(counters, key)
    if body.get("wifi") is not None:
        wifi = _require_object(body.get("wifi"), "wifi")
        if wifi.get("rssi_dbm") is not None:
            _require_int(wifi, "rssi_dbm")
    return body


def validate_event_payload(payload: Any) -> Dict[str, Any]:
    body = _validate_common(payload, "hear/event")
    _require_string(body, "event_type")
    _require_int(body, "event_seq")
    _require_object(body.get("event"), "event")
    return body


def make_handler(store: HeartbeatReceiverStore,
                 max_body_bytes: int = MAX_BODY_BYTES) -> type[BaseHTTPRequestHandler]:
    class ReceiverHandler(BaseHTTPRequestHandler):
        server_version = "hear-heartbeat-receiver/1"

        def do_GET(self) -> None:
            if urlparse(self.path).path != "/healthz":
                self.send_error(404)
                return
            self.send_json({
                "status": "ok",
                "service": "hear-heartbeat-receiver",
                "redis_target": store.redis_target,
            })

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            routes: Dict[str, tuple[Callable[[Any], Dict[str, Any]], Callable[[Dict[str, Any]], Dict[str, Any]]]] = {
                "/api/hear/heartbeat": (validate_heartbeat_payload, store.write_heartbeat),
                "/api/hear/event": (validate_event_payload, store.write_event),
            }
            route = routes.get(path)
            if route is None:
                self.send_error(404)
                return
            validator, writer = route
            try:
                writer(validator(self.read_json_body(max_body_bytes)))
            except RequestError as exc:
                self.send_json({"error": str(exc)}, exc.status)
                return
            except Exception as exc:  # pragma: no cover - exercised with a fake failing store.
                self.send_json({"error": "redis write failed", "detail": str(exc)}, 503)
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def read_json_body(self, max_body_bytes: int) -> Any:
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                raise RequestError("missing Content-Length")
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise RequestError("invalid Content-Length") from exc
            if length < 0:
                raise RequestError("invalid Content-Length")
            if length > max_body_bytes:
                raise RequestError(
                    f"payload too large ({length} > {max_body_bytes} bytes)", status=413)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise RequestError("truncated request body")
            try:
                return json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise RequestError("request body must be UTF-8 JSON") from exc
            except json.JSONDecodeError as exc:
                raise RequestError("malformed JSON") from exc

        def send_json(self, data: Mapping[str, Any], code: int = 200) -> None:
            body = json.dumps(data, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return ReceiverHandler


def make_redis_client(redis_host: str = REDIS_HOST, redis_port: int = REDIS_PORT,
                      redis_pass: Optional[str] = REDIS_PASS) -> redis.Redis:
    return redis.Redis(
        host=redis_host,
        port=redis_port,
        password=redis_pass or None,
        decode_responses=True,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
        retry_on_timeout=False,
    )


def create_server(bind: str, port: int, store: HeartbeatReceiverStore,
                  max_body_bytes: int = MAX_BODY_BYTES) -> ReceiverServer:
    return ReceiverServer((bind, port), make_handler(store, max_body_bytes=max_body_bytes), store)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=API_PORT)
    ap.add_argument("--redis-host", default=REDIS_HOST)
    ap.add_argument("--redis-port", type=int, default=REDIS_PORT)
    ap.add_argument("--redis-pass", default=REDIS_PASS)
    ap.add_argument("--heartbeat-ttl", type=int, default=HEARTBEAT_TTL_S)
    ap.add_argument("--max-body-bytes", type=int, default=MAX_BODY_BYTES)
    ap.add_argument("--event-stream-key", default=EVENT_STREAM_KEY)
    ap.add_argument("--event-stream-maxlen", type=int, default=EVENT_STREAM_MAXLEN)
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    target = f"{args.redis_host}:{args.redis_port}"
    store = HeartbeatReceiverStore(
        make_redis_client(args.redis_host, args.redis_port, args.redis_pass),
        heartbeat_ttl_s=args.heartbeat_ttl,
        event_stream_key=args.event_stream_key,
        event_stream_maxlen=args.event_stream_maxlen,
        redis_target=target,
    )
    print(f"[hear-heartbeat] Redis target configured -> {target}")
    print(f"[hear-heartbeat] Listening on http://{args.bind}:{args.port}")
    create_server(args.bind, args.port, store, max_body_bytes=args.max_body_bytes).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
