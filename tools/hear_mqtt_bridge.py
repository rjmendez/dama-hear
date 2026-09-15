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
import ssl
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.hear_heartbeat_receiver import (  # noqa: E402
    DurablePruneWorker,
    DurableReplayWorker,
    HeartbeatReceiverStore,
    RequestError,
    add_durable_store_args,
    make_durable_store,
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

# TLS/mTLS material. In Kubernetes these are populated by mounting a Secret (see
# deploy/k8s/hear-mqtt-bridge.yaml) with the broker's CA bundle plus this client's own
# certificate/key pair, so the broker can also authenticate *us* (mutual TLS), not just
# the other way around.
MQTT_CA_CERTS = os.environ.get("MQTT_CA_CERTS")
MQTT_CERTFILE = os.environ.get("MQTT_CERTFILE")
MQTT_KEYFILE = os.environ.get("MQTT_KEYFILE")
MQTT_TLS_INSECURE = os.environ.get("MQTT_TLS_INSECURE", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
MQTT_TLS_VERSION = os.environ.get("MQTT_TLS_VERSION", "tlsv1_2")

_TLS_VERSIONS: Dict[str, int] = {
    "tlsv1_2": ssl.PROTOCOL_TLS_CLIENT if hasattr(ssl, "PROTOCOL_TLS_CLIENT") else ssl.PROTOCOL_TLSv1_2,
    "tlsv1.2": ssl.PROTOCOL_TLS_CLIENT if hasattr(ssl, "PROTOCOL_TLS_CLIENT") else ssl.PROTOCOL_TLSv1_2,
    # Modern OpenSSL negotiates the highest mutually supported protocol under
    # PROTOCOL_TLS_CLIENT (which itself enforces a >=TLSv1.2 floor), so "tlsv1_3" is
    # accepted as a floor label too rather than requiring a dedicated (and, in current
    # Python/OpenSSL, non-existent) PROTOCOL_TLSv1_3 constant.
    "tlsv1_3": ssl.PROTOCOL_TLS_CLIENT if hasattr(ssl, "PROTOCOL_TLS_CLIENT") else ssl.PROTOCOL_TLSv1_2,
    "tlsv1.3": ssl.PROTOCOL_TLS_CLIENT if hasattr(ssl, "PROTOCOL_TLS_CLIENT") else ssl.PROTOCOL_TLSv1_2,
}

_ROUTES: Dict[str, Tuple[Callable[[Any], Dict[str, Any]], str]] = {
    "hear/heartbeat": (validate_heartbeat_payload, "write_heartbeat"),
    "hear/event": (validate_event_payload, "write_event"),
}


class TlsConfigError(ValueError):
    """Raised when TLS/mTLS flags are inconsistent or unsafe."""


def resolve_tls_version(name: str) -> int:
    """Map a --tls-version flag value to an ssl.PROTOCOL_* constant."""
    key = name.strip().lower()
    try:
        return _TLS_VERSIONS[key]
    except KeyError as exc:
        raise TlsConfigError(
            f"unsupported --tls-version {name!r}; choose one of {sorted(_TLS_VERSIONS)}"
        ) from exc


def configure_tls(client: mqtt.Client, *, ca_certs: Optional[str] = None,
                   certfile: Optional[str] = None, keyfile: Optional[str] = None,
                   tls_insecure: bool = False, tls_version: str = MQTT_TLS_VERSION) -> bool:
    """Configure TLS (and mTLS, if a client cert/key pair is supplied) on ``client``.

    Returns True if TLS was enabled, False if no CA bundle was supplied (plaintext).
    Raises TlsConfigError if only one of certfile/keyfile is given, since a half-configured
    client certificate is never valid for mTLS.
    """
    if (certfile is None) != (keyfile is None):
        raise TlsConfigError(
            "--certfile and --keyfile must both be supplied together for mTLS, or both omitted"
        )
    if not ca_certs:
        if certfile or keyfile:
            raise TlsConfigError("--ca-certs is required when --certfile/--keyfile are set")
        return False

    client.tls_set(
        ca_certs=ca_certs,
        certfile=certfile,
        keyfile=keyfile,
        tls_version=resolve_tls_version(tls_version),
    )
    # tls_insecure_set(True) disables hostname verification against the broker's cert,
    # defeating the point of TLS; only ever set it from an explicit, logged opt-in.
    if tls_insecure:
        logger.warning(
            "TLS hostname/certificate verification DISABLED (--tls-insecure); "
            "this bridge is vulnerable to MITM while this flag is set"
        )
    client.tls_insecure_set(tls_insecure)
    return True


def _uptime_fallback_ts_ms(payload: Mapping[str, Any]) -> bool:
    """True when ``ts_ms`` is the node's own uptime-derived placeholder, not a wall clock.

    hear_push_ts_ms() returns ``uptime_s * 1000 + 1`` while the clock is invalid, purely to clear
    the upstream ingest gate that rejects a non-positive numeric timestamp. Matching that exact
    arithmetic is how a body carrying no time block at all (pre-#156 event firmware) is still
    recognised as having had no clock: the only wall clock that could collide is a 1970 one.
    """
    ts_ms = payload.get("ts_ms")
    uptime_s = payload.get("uptime_s")
    if isinstance(ts_ms, bool) or isinstance(uptime_s, bool):
        return False
    if not isinstance(ts_ms, (int, float)) or not isinstance(uptime_s, int):
        return False
    return int(ts_ms) == uptime_s * 1000 + 1


def _stated_time_is_invalid(payload: Mapping[str, Any]) -> bool:
    """True when the payload states -- or, for a legacy body, demonstrates -- an invalid clock."""
    time_state = payload.get("time")
    if isinstance(time_state, Mapping):
        return time_state.get("valid") is False
    # A pre-#156 firmware sends no time block on hear/event at all. Its uptime-derived ts_ms
    # placeholder is the same "I have no clock" statement the time block would have carried, so
    # the #185 fix has to cover it too or the upstream-rewritten ts is taken at face value.
    return "time" not in payload and _uptime_fallback_ts_ms(payload)


def device_id_from_topic(topic: str) -> Optional[str]:
    """Extract the device_id segment from a ``dama/<device_id>/telemetry`` topic."""
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "dama" and parts[2] == "telemetry" and parts[1]:
        return parts[1]
    return None


class BridgeStats:
    """Plain counters for observability/tests. Not persisted anywhere."""

    def __init__(self) -> None:
        self.accepted = 0
        self.ignored = 0
        self.rejected = 0
        self.malformed = 0
        self.impersonation = 0
        # Refused messages that were durably quarantined rather than only logged (R4).
        self.quarantined = 0


def dispatch_message(store: HeartbeatReceiverStore, raw: bytes, topic: str,
                     stats: Optional[BridgeStats] = None) -> Optional[Dict[str, Any]]:
    """Decode and route one MQTT message payload; never raises."""
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
    telemetry_path = str(payload.get("telemetry_path"))
    # The bytes as received, captured before any ts rewrite below: the quarantine is evidence of
    # what the publisher sent, so it must not hold this bridge's repaired copy.
    raw_payload = raw

    def quarantine(reason: str) -> None:
        # The topic segment first: it is the only identity the broker ties to the publishing
        # connection, so grouping and rate-limiting on it cannot be evaded by rotating the
        # device_id in the body.
        device_id = device_id_from_topic(topic)
        if not device_id:
            claimed = payload.get("device_id")
            device_id = claimed.strip() if isinstance(claimed, str) and claimed.strip() else "unknown"
        entry = store.record_refusal(telemetry_path, device_id, "mqtt_bridge",
                                     reason, raw_payload)
        if entry is not None:
            stats.quarantined += 1

    try:
        ts = payload.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            payload = dict(payload)
            if _stated_time_is_invalid(payload):
                # A node without a GPS fix sends "ts":null and a monotonic
                # "ts_ms" (uptime-derived) purely to clear the upstream ingest
                # gate, which rejects a non-positive numeric timestamp. That
                # ingest then copies ts_ms back over the null ts, so what
                # arrives here claims a wall-clock time the node never had.
                # Restore the node's own contract -- no valid clock, no ts --
                # so the heartbeat lands as degraded-but-visible instead of
                # being rejected outright. received_at stays the trustworthy
                # time for these records.
                payload["ts"] = None
            else:
                payload["ts"] = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(
                    timespec="seconds").replace("+00:00", "Z")
        body = validator(payload)
    except RequestError as exc:
        stats.rejected += 1
        logger.warning("rejected %s message on %s: %s", payload.get("telemetry_path"), topic, exc)
        quarantine(str(exc))
        return None
    except (ValueError, OverflowError, OSError) as exc:
        stats.rejected += 1
        logger.warning("rejected %s message on %s: unusable numeric ts: %s",
                       payload.get("telemetry_path"), topic, exc)
        quarantine(f"unusable numeric ts: {exc}")
        return None

    # The broker's topic is the only thing this bridge can trust to belong to the
    # publishing connection; a device (or an on-path attacker with an MQTT credential
    # but no TLS client cert for another device) could otherwise claim any device_id it
    # likes in the JSON body. Reject any mismatch instead of trusting the payload alone.
    topic_device_id = device_id_from_topic(topic)
    payload_device_id = body.get("device_id")
    if topic_device_id is not None and topic_device_id != payload_device_id:
        stats.impersonation += 1
        logger.warning(
            "rejected %s message: topic device_id %r does not match payload device_id %r "
            "(possible impersonation) on %s",
            payload.get("telemetry_path"), topic_device_id, payload_device_id, topic,
        )
        quarantine(
            f"topic device_id {topic_device_id!r} does not match payload device_id "
            f"{payload_device_id!r}")
        return None

    writer: Callable[[Dict[str, Any]], Dict[str, Any]] = getattr(store, writer_name)
    record = writer(body)
    stats.accepted += 1
    return record


def make_client(store: HeartbeatReceiverStore, topic: str = MQTT_TOPIC,
                client_id: str = MQTT_CLIENT_ID,
                stats: Optional[BridgeStats] = None,
                ca_certs: Optional[str] = MQTT_CA_CERTS,
                certfile: Optional[str] = MQTT_CERTFILE,
                keyfile: Optional[str] = MQTT_KEYFILE,
                tls_insecure: bool = MQTT_TLS_INSECURE,
                tls_version: str = MQTT_TLS_VERSION) -> mqtt.Client:
    stats = stats if stats is not None else BridgeStats()
    client = mqtt.Client(client_id=client_id, clean_session=True)
    tls_enabled = configure_tls(
        client,
        ca_certs=ca_certs,
        certfile=certfile,
        keyfile=keyfile,
        tls_insecure=tls_insecure,
        tls_version=tls_version,
    )
    client.tls_enabled = tls_enabled
    if not tls_enabled:
        logger.warning(
            "MQTT TLS is DISABLED (no --ca-certs configured); connecting in plaintext"
        )

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
    ap.add_argument("--ca-certs", default=MQTT_CA_CERTS,
                    help="Path to the CA bundle (PEM) used to verify the broker's certificate. "
                         "Enables TLS. Typically a mounted Kubernetes Secret path.")
    ap.add_argument("--certfile", default=MQTT_CERTFILE,
                    help="Path to this client's TLS certificate (PEM) for mTLS. "
                         "Requires --keyfile and --ca-certs.")
    ap.add_argument("--keyfile", default=MQTT_KEYFILE,
                    help="Path to this client's TLS private key (PEM) for mTLS. "
                         "Requires --certfile and --ca-certs.")
    ap.add_argument("--tls-insecure", action="store_true", default=MQTT_TLS_INSECURE,
                    help="DANGEROUS: disable broker hostname/certificate verification. "
                         "Never use outside of local testing.")
    ap.add_argument("--tls-version", default=MQTT_TLS_VERSION,
                    choices=sorted(_TLS_VERSIONS),
                    help="Minimum TLS protocol version to negotiate with the broker.")
    add_durable_store_args(ap)
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[hear-mqtt-bridge] %(message)s")
    args = parse_args(argv)
    redis_target = f"{args.redis_host}:{args.redis_port}"
    store = HeartbeatReceiverStore(
        make_redis_client(args.redis_host, args.redis_port, args.redis_pass),
        redis_target=redis_target,
        durable_store=make_durable_store(args.durable_store, args.durable_db),
    )
    replay = store.replay_pending(limit=args.durable_replay_limit)
    logger.info("Redis target -> %s", redis_target)
    logger.info("durable backend=%s path=%s", store.durable_store.backend, store.durable_store.path)
    if replay["attempted"] or replay["failed"]:
        logger.info("replay pending attempted=%d synced=%d failed=%d remaining=%d",
                    replay["attempted"], replay["synced"], replay["failed"],
                    replay["remaining_pending"])
    # Keeps replaying pending durable records for the life of the process, not only at this
    # startup catch-up above, so a Redis outage that outlasts a few messages still self-heals
    # once Redis recovers without requiring a bridge restart.
    replay_worker = DurableReplayWorker(
        store, args.durable_replay_interval_s, args.durable_replay_limit,
        name="hear-mqtt-bridge-replay")
    prune_worker = DurablePruneWorker(
        store, args.durable_prune_interval_s, args.durable_retention_days,
        name="hear-mqtt-bridge-prune")
    logger.info("MQTT broker -> %s:%s topic=%s", args.mqtt_host, args.mqtt_port, args.mqtt_topic)
    try:
        client = make_client(
            store, topic=args.mqtt_topic, client_id=args.mqtt_client_id,
            ca_certs=args.ca_certs, certfile=args.certfile, keyfile=args.keyfile,
            tls_insecure=args.tls_insecure, tls_version=args.tls_version,
        )
    except TlsConfigError as exc:
        logger.error("invalid TLS configuration: %s", exc)
        return 2
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=args.mqtt_keepalive_s)
    try:
        client.loop_forever()
    finally:
        replay_worker.stop()
        prune_worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
