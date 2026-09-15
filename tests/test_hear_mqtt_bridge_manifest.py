"""The MQTT bridge manifest: durable outbox plus Redis compatibility bridge."""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "k8s" / "hear-mqtt-bridge.yaml"


def _docs():
    return list(yaml.safe_load_all(MANIFEST.read_text()))


def _doc(kind):
    return next(d for d in _docs() if d["kind"] == kind)


def test_it_declares_one_pvc_and_one_deployment():
    assert [d["kind"] for d in _docs()] == ["PersistentVolumeClaim", "Deployment"]


def test_the_bridge_runs_the_expected_entrypoint_and_mounts_code_and_state():
    dep = _doc("Deployment")
    spec = dep["spec"]["template"]["spec"]
    assert spec["hostNetwork"] is True
    assert spec["dnsPolicy"] == "ClusterFirstWithHostNet"
    c = spec["containers"][0]
    assert c["name"] == "bridge"
    assert c["image"] == "python:3.13-slim"
    assert "hear_mqtt_bridge.py" in c["args"][0]
    mounts = {(m["name"], m.get("subPath", m["mountPath"])): m["mountPath"] for m in c["volumeMounts"]}
    assert mounts[("code", "tools_hear_heartbeat_receiver.py")] == "/app/tools/hear_heartbeat_receiver.py"
    assert mounts[("code", "tools_hear_mqtt_bridge.py")] == "/app/tools/hear_mqtt_bridge.py"
    assert mounts[("state", "/state")] == "/state"


def test_it_declares_the_expected_environment_and_durable_sqlite_outbox():
    dep = _doc("Deployment")
    spec = dep["spec"]["template"]["spec"]
    env = {item["name"]: item for item in spec["containers"][0]["env"]}
    assert env["MQTT_HOST"]["value"] == "127.0.0.1"
    assert env["MQTT_PORT"]["value"] == "31883"
    assert env["MQTT_TOPIC"]["value"] == "dama/+/telemetry"
    assert env["REDIS_HOST"]["value"] == "audit-redis.infra.svc.cluster.local"
    assert env["REDIS_PORT"]["value"] == "6379"
    assert env["HEAR_DURABLE_STORE"]["value"] == "sqlite"
    assert env["HEAR_DURABLE_DB"]["value"] == "/state/mqtt-bridge.sqlite3"
    assert env["HEAR_DURABLE_REPLAY_LIMIT"]["value"] == "256"
    assert env["HEAR_DURABLE_REPLAY_MAX_BATCHES"]["value"] == "1024"
    assert env["HEAR_DURABLE_REPLAY_INTERVAL_S"]["value"] == "60"
    assert env["REDIS_PASS"]["valueFrom"]["secretKeyRef"] == {
        "name": "dama-redis-secret",
        "key": "REDIS_PASS",
        "optional": True,
    }
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["state"]["persistentVolumeClaim"] == {"claimName": "hear-mqtt-bridge-state"}


def test_it_declares_a_dedicated_state_pvc():
    pvc = _doc("PersistentVolumeClaim")
    assert pvc["metadata"]["name"] == "hear-mqtt-bridge-state"
    assert pvc["metadata"]["namespace"] == "dama"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "5Gi"
