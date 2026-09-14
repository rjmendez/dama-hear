#!/usr/bin/env python3
"""Bridges hear_node telemetry that arrives over MQTT into dama:hear:* Redis state.

The fleet's LAN subnet turned out unreachable inbound from this cluster (see
hear-heartbeat.yaml), so hear_node now pushes to dama-gotchi's public AWS ingest API
instead: hear_node -> API Gateway -> batch_ingest Lambda -> SQS -> forwarder Lambda ->
Oxalis SQS queue -> dama-sqs-consumer.py (already running, unrelated to this repo) ->
this cluster's local mqtt-broker, topic `dama/<device_id>/telemetry`.

That broker also carries dama-gotchi's own phone/GNSS telemetry on the same topic
pattern, keyed only by device_id -- this bridge must ignore anything that is not a
hear_node message rather than error on it. It reuses hear_heartbeat_receiver's own
validate_*/write_* logic (rather than re-implementing the schema) specifically so the
LAN and AWS paths can never drift onto incompatible Redis shapes.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.hear_heartbeat_receiver import (  # noqa: E402
    HeartbeatReceiverStore,
    RequestError,
    make_redis_client,
    validate_event_payload,
    validate_heartbeat_payload,
)

logger = logging.getLogger("hear_mqtt_bridge")

MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker.agents.svc.cluster.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "dama/+/telemetry")
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "hear-mqtt-bridge")
MQTT_KEEPALIVE_S = int(os.environ.get("MQTT_KEEPALIVE_S", "60"))

REDIS_HOST = os.environ.get("REDIS_HOST", "audit-redis.infra.svc.cluster.local")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASS = os.environ.get("REDIS_PASS")

# telemetry_path -> (validator, HeartbeatReceiverStore method name). Kept as a lookup table
# (not if/elif) so adding a third hear_node message kind is one line here, not a new branch
# in dispatch_message().
_ROUTES: Dict[str, Tuple[Callable[[Any], Dict[str, Any]], str]] = {
    "hear/heartbeat": (validate_heartbeat_payload, "write_heartbeat"),
    "hear/event": (validate_event_payload, "write_event"),
}


class BridgeStats:
    """Plain counters for observability/tests. Not persisted anywhere."""

    def __init__(self) -> None:
        self.accepted = 0
        self.ignored = 0
        self.rejected = 0
        self.malformed = 0


def dispatch_message(store: HeartbeatReceiverStore, raw: bytes, topic: str,
                      stats: Optional[BridgeStats] = None) -> Optional[Dict[str, Any]]:
    """Decode and route one MQTT message payload; never raises.

    A malformed payload, a message for a device that isn't a hear_node (this topic
    pattern also carries dama-gotchi's own telemetry), or one that fails schema
    validation must not crash the bridge or block the messages behind it -- so every
    failure path here logs and returns None instead of propagating.
    """
    stats = stats if stats is not None else BridgeStats()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        stats.malformed += 1
        logger.warning("dropping malformed message on %s: %s", topic, exc)
        return None
    if not isinstance(payload, dict):
        stats.malformed += 1
        logger.warning("dropping non-object message on %s", topic)
        return None

    route = _ROUTES.get(payload.get("telemetry_path"))
    if route is None:
        stats.ignored += 1
        return None
    validator, writer_name = route

    try:
        # batch_ingest/forwarder normalize the transport timestamp to numeric
        # milliseconds. The Redis contract uses an RFC3339 string.
        ts = payload.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            payload = dict(payload)
            payload["ts"] = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        body = validator(payload)
    except RequestError as exc:
        stats.rejected += 1
        logger.warning("rejected %s message on %s: %s", payload.get("telemetry_path"), topic, exc)
        return None

    writer: Callable[[Dict[str, Any]], Dict[str, Any]] = getattr(store, writer_name)
    record = writer(body)
    stats.accepted += 1
    return record


def make_client(store: HeartbeatReceiverStore, topic: str = MQTT_TOPIC,
                client_id: str = MQTT_CLIENT_ID,
                stats: Optional[BridgeStats] = None) -> mqtt.Client:
    stats = stats if stats is not None else BridgeStats()
    client = mqtt.Client(client_id=client_id, clean_session=True)

    def _on_connect(c, userdata, flags, rc, *args):
        if rc != 0:
            logger.error("mqtt connect failed rc=%s", rc)
            return
        logger.info("connected; subscribing to %s", topic)
        c.subscribe(topic, qos=1)

    def _on_message(c, userdata, msg):
        dispatch_message(store, msg.payload, msg.topic, stats)

    def _on_disconnect(c, userdata, rc, *args):
        if rc != 0:
            logger.warning("unexpected mqtt disconnect rc=%s; client will auto-reconnect", rc)

    client.on_connect = _on_connect
    client.on_message = _on_message
    client.on_disconnect = _on_disconnect
    client.stats = stats
    return client


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mqtt-host", default=MQTT_HOST)
    ap.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    ap.add_argument("--mqtt-topic", default=MQTT_TOPIC)
    ap.add_argument("--mqtt-client-id", default=MQTT_CLIENT_ID)
    ap.add_argument("--mqtt-keepalive-s", type=int, default=MQTT_KEEPALIVE_S)
    ap.add_argument("--redis-host", default=REDIS_HOST)
    ap.add_argument("--redis-port", type=int, default=REDIS_PORT)
    ap.add_argument("--redis-pass", default=REDIS_PASS)
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[hear-mqtt-bridge] %(message)s")
    args = parse_args(argv)
    redis_target = f"{args.redis_host}:{args.redis_port}"
    store = HeartbeatReceiverStore(
        make_redis_client(args.redis_host, args.redis_port, args.redis_pass),
        redis_target=redis_target,
    )
    client = make_client(store, topic=args.mqtt_topic, client_id=args.mqtt_client_id)
    logger.info("Redis target -> %s", redis_target)
    logger.info("MQTT broker -> %s:%s topic=%s", args.mqtt_host, args.mqtt_port, args.mqtt_topic)
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=args.mqtt_keepalive_s)
    client.loop_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
