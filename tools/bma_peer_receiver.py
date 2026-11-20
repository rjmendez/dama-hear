#!/usr/bin/env python3
"""Optional, dedicated MQTT subscriber for locally retaining BMA metadata."""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hear.ingest.bma_peer import (  # noqa: E402
    BmaIdentityConflict,
    BmaPeerError,
    BmaPeerStore,
    BmaStorageFull,
    BmaValidationError,
    DEFAULT_MAX_MESSAGE_BYTES,
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_STORAGE_BYTES,
    DEFAULT_RETENTION_S,
    normalize_device_id,
)

LOG = logging.getLogger("bma_peer_receiver")
DEFAULT_DB = os.environ.get("BMA_PEER_DB", "bma-peer.sqlite3")
RECONNECT_INITIAL_DELAY_S = 1
RECONNECT_MAX_DELAY_S = 30


class TlsConfigError(ValueError):
    """Raised when a broker connection would not have verified TLS."""


def _is_loopback(host: str) -> bool:
    candidate = host.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def configure_tls(client, host: str, ca_file: Optional[str] = None) -> bool:
    """Allow plaintext only on loopback; otherwise install a verified CA context."""
    if ca_file is None:
        if not _is_loopback(host):
            raise TlsConfigError("non-loopback brokers require --ca-file for verified TLS")
        return False
    path = Path(ca_file)
    if not path.is_file():
        raise TlsConfigError("CA file must be a readable regular file")
    try:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(path))
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise TlsConfigError("cannot load a valid CA bundle: %s" % exc) from exc
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise TlsConfigError("TLS context must verify the broker certificate and hostname")
    try:
        client.tls_set_context(context)
        client.tls_insecure_set(False)
    except (AttributeError, OSError, ssl.SSLError, TypeError, ValueError) as exc:
        raise TlsConfigError("cannot configure verified MQTT TLS: %s" % exc) from exc
    return True


def load_paho_client(client_id: str):
    """Import the optional dependency only for `run`, requiring manual acknowledgements."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise RuntimeError(
            "MQTT mode requires optional dependency paho-mqtt; install it in this environment"
        ) from exc
    try:
        options = {
            "client_id": client_id,
            "clean_session": False,
            "manual_ack": True,
        }
        callback_api = getattr(mqtt, "CallbackAPIVersion", None)
        if callback_api is not None:
            options["callback_api_version"] = callback_api.VERSION2
        client = mqtt.Client(**options)
    except TypeError as exc:
        raise RuntimeError(
            "MQTT mode requires a paho-mqtt version supporting manual_ack"
        ) from exc
    return client


def make_client(store: BmaPeerStore, *, host: str, port: int, topic: str,
                client_id: str, ca_file: Optional[str] = None,
                expected_site_id: Optional[str] = None):
    """Build the dedicated subscriber; only a committed or refused message is acked."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("MQTT port must be from 1 to 65535")
    if topic != "dama/+/telemetry":
        raise ValueError("receiver topic must remain dama/+/telemetry")
    if (not isinstance(client_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,23}", client_id, re.ASCII) is None):
        raise ValueError("MQTT client ID must be stable, ASCII, and 1-23 characters")
    client = load_paho_client(client_id)
    configure_tls(client, host, ca_file)
    client.retry_requested = threading.Event()
    client.subscription_failed = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        reason = getattr(reason_code, "value", reason_code)
        if type(reason) is not int or reason != 0:
            LOG.error("MQTT connection refused: %s", reason_code)
            client.subscription_failed.set()
            client.disconnect()
            return
        result, _mid = client.subscribe(topic, qos=1)
        if result != 0:
            LOG.error("MQTT subscribe failed: %s", result)
            client.subscription_failed.set()
            client.disconnect()

    def on_subscribe(client, userdata, mid, granted_qos, properties=None):
        try:
            grants = tuple(getattr(code, "value", code) for code in granted_qos)
        except TypeError:
            grants = ()
        if len(grants) != 1 or type(grants[0]) is not int or grants[0] != 1:
            LOG.error("broker did not grant QoS 1 for %s (SUBACK: %s)", topic, grants)
            client.subscription_failed.set()
            client.disconnect()
            return
        LOG.info("subscribed to %s with QoS 1", topic)

    def on_message(client, userdata, message):
        if not is_bma_topic(message.topic):
            if not _looks_like_bma_message(message.payload):
                _ack(client, message)
                return
        try:
            result = store.accept(
                message.payload, message.topic, expected_site_id=expected_site_id)
        except (BmaValidationError, BmaIdentityConflict) as exc:
            # Invalid messages are deterministic poison; acknowledge after refusal so that
            # they cannot indefinitely block later records on the dedicated subscription.
            LOG.warning("refused BMA MQTT message on %s: %s", message.topic, exc)
        except (BmaStorageFull, BmaPeerError, OSError) as exc:
            if message.qos == 0:
                LOG.error(
                    "lost QoS 0 BMA MQTT delivery on %s: storage failed; "
                    "no broker redelivery: %s", message.topic, exc)
                return
            # Ask the supervisor to reconnect so a persistent broker session can redeliver.
            # Never acknowledge or silently discard data that failed to commit.
            LOG.error("BMA MQTT store unavailable; leaving delivery unacknowledged: %s", exc)
            client.retry_requested.set()
            try:
                client.disconnect()
            except OSError as disconnect_error:
                LOG.error("MQTT disconnect after storage failure failed: %s", disconnect_error)
            return
        else:
            if message.qos == 0:
                LOG.info("%s BMA observation %s (best-effort QoS 0 delivery)",
                         result["status"], result["det_id"])
            else:
                LOG.info("%s BMA observation %s", result["status"], result["det_id"])
        _ack(client, message)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    return client


def is_bma_topic(topic: str) -> bool:
    parts = topic.split("/") if isinstance(topic, str) else []
    return (len(parts) == 3 and parts[0] == "dama" and parts[2] == "telemetry"
            and receiver_device_is_bma(parts[1]))


def receiver_device_is_bma(device_id: str) -> bool:
    try:
        return normalize_device_id(device_id).startswith("bma-")
    except BmaValidationError:
        return False


def _looks_like_bma_message(raw) -> bool:
    """Only parse unrelated topics enough to catch BMA records misrouted by identity."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > DEFAULT_MAX_MESSAGE_BYTES:
        return False
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    if not isinstance(payload, dict):
        return False
    device_id = payload.get("device_id")
    return (payload.get("schema") == "bma.bearing.v1"
            or payload.get("telemetry_path") == "bma/bearing"
            or (isinstance(device_id, str) and receiver_device_is_bma(device_id)))


def _ack(client, message) -> None:
    if message.qos == 0:
        return
    ack_result = client.ack(message.mid, message.qos)
    if ack_result != 0:
        LOG.error("failed to acknowledge MQTT message mid=%s: %s", message.mid, ack_result)


def _run(args) -> int:
    try:
        store = BmaPeerStore(
            args.db, max_records=args.max_records, max_storage_bytes=args.max_storage_bytes,
            retention_s=args.retention_s, max_message_bytes=args.max_message_bytes)
        client = make_client(
            store, host=args.mqtt_host, port=args.mqtt_port, topic=args.mqtt_topic,
            client_id=args.mqtt_client_id, ca_file=args.ca_file,
            expected_site_id=args.site_id)
    except (BmaPeerError, TlsConfigError, RuntimeError, ValueError) as exc:
        LOG.error("%s", exc)
        return 2
    try:
        reconnecting = False
        delay_s = RECONNECT_INITIAL_DELAY_S
        while True:
            try:
                client.connect(args.mqtt_host, args.mqtt_port, keepalive=args.keepalive_s)
                loop_result = client.loop_forever()
            except KeyboardInterrupt:
                LOG.info("stopped by operator")
                return 0
            except OSError as exc:
                if not reconnecting:
                    LOG.error("MQTT connection failed: %s", exc)
                    return 2
                LOG.error("MQTT reconnect failed; retrying in %ss: %s", delay_s, exc)
                time.sleep(delay_s)
                delay_s = min(delay_s * 2, RECONNECT_MAX_DELAY_S)
                continue
            if client.subscription_failed.is_set():
                LOG.error("broker refused the required QoS 1 subscription; stopping")
                return 2
            if client.retry_requested.is_set():
                client.retry_requested.clear()
                reconnecting = True
                LOG.warning("reconnecting to retry unacknowledged BMA delivery in %ss", delay_s)
                time.sleep(delay_s)
                delay_s = min(delay_s * 2, RECONNECT_MAX_DELAY_S)
                continue
            if loop_result not in (None, 0):
                LOG.error("MQTT loop stopped with error %s", loop_result)
                return 2
            return 0
    except KeyboardInterrupt:
        LOG.info("stopped by operator")
        return 0


def _inspect(args) -> int:
    try:
        store = BmaPeerStore(
            args.db, max_records=args.max_records, max_storage_bytes=args.max_storage_bytes,
            retention_s=args.retention_s, max_message_bytes=args.max_message_bytes)
        print(json.dumps({
            "limits": {"max_records": store.max_records,
                       "max_storage_bytes": store.max_storage_bytes,
                       "retention_s": store.retention_s},
            "usage": store.counts(),
            "observations": store.list_observations(limit=args.limit),
            "interpretation": "BMA bearings are unvalidated; source wall time is display-only.",
        }, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    except (BmaPeerError, OSError, sqlite3.DatabaseError) as exc:
        LOG.error("%s", exc)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="subscribe to the dedicated BMA MQTT topic")
    run.add_argument("--enable", action="store_true",
                     help="required explicit opt-in; this command opens an MQTT connection")
    run.add_argument("--mqtt-host", default=os.environ.get("BMA_PEER_MQTT_HOST", "127.0.0.1"))
    run.add_argument("--mqtt-port", type=int, default=int(os.environ.get("BMA_PEER_MQTT_PORT", "1883")))
    run.add_argument("--mqtt-topic", default="dama/+/telemetry")
    run.add_argument("--mqtt-client-id", default="dama-bma-peer")
    run.add_argument("--keepalive-s", type=int, default=30)
    run.add_argument("--ca-file", default=os.environ.get("BMA_PEER_CA_FILE"))
    run.add_argument("--site-id", default=os.environ.get("BMA_PEER_SITE_ID"))
    _add_store_arguments(run)
    inspect = subparsers.add_parser("inspect", help="print locally stored BMA observations")
    inspect.add_argument("--limit", type=int, default=100)
    _add_store_arguments(inspect)
    return parser


def _add_store_arguments(parser):
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--max-storage-bytes", type=int, default=DEFAULT_MAX_STORAGE_BYTES)
    parser.add_argument("--max-message-bytes", type=int, default=DEFAULT_MAX_MESSAGE_BYTES)
    parser.add_argument("--retention-s", type=int, default=DEFAULT_RETENTION_S)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        if not args.enable:
            parser.error("run opens MQTT only with explicit --enable")
        if args.keepalive_s <= 0:
            parser.error("--keepalive-s must be positive")
        return _run(args)
    return _inspect(args)


if __name__ == "__main__":
    sys.exit(main())
