"""The heartbeat receiver manifest: reachable from nodes, token-gated, and wired to the new entrypoint."""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "k8s" / "hear-heartbeat.yaml"


def _docs():
    return list(yaml.safe_load_all(MANIFEST.read_text()))


def _doc(kind):
    return next(d for d in _docs() if d["kind"] == kind)


def test_it_declares_one_pvc_one_deployment_and_one_service():
    docs = _docs()
    kinds = [d["kind"] for d in docs]
    assert kinds == ["PersistentVolumeClaim", "Deployment", "Service"]


def test_the_receiver_runs_the_new_entrypoint_and_mounts_its_bundle_and_state():
    dep = _doc("Deployment")
    spec = dep["spec"]["template"]["spec"]
    assert spec["hostNetwork"] is True
    assert spec["dnsPolicy"] == "ClusterFirstWithHostNet"
    c = spec["containers"][0]
    assert c["name"] == "receiver"
    assert c["image"] == "python:3.13-slim"
    assert "hear_heartbeat_receiver.py --port 5051" in c["args"][0]
    mounts = {(m["name"], m.get("subPath", m["mountPath"])): m["mountPath"] for m in c["volumeMounts"]}
    assert mounts[("code", "hear__init__.py")] == "/app/hear/__init__.py"
    assert mounts[("code", "hear_ingest__init__.py")] == "/app/hear/ingest/__init__.py"
    assert mounts[("code", "hear_ingest_envelope.py")] == "/app/hear/ingest/envelope.py"
    assert mounts[("code", "hear_ingest_batch.py")] == "/app/hear/ingest/batch.py"
    assert mounts[("code", "hear_ingest_observability.py")] == "/app/hear/ingest/observability.py"
    assert mounts[("code", "tools_hear_heartbeat_receiver.py")] == "/app/tools/hear_heartbeat_receiver.py"
    assert mounts[("state", "/state")] == "/state"


def test_it_exposes_the_http_port_on_the_host_and_health_probe():
    dep, svc = _doc("Deployment"), _doc("Service")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["ports"] == [{"name": "http", "containerPort": 5051, "hostPort": 5051}]
    for probe_name in ("livenessProbe", "readinessProbe"):
        probe = c[probe_name]
        assert probe["httpGet"]["path"] == "/healthz"
        assert probe["httpGet"]["port"] == "http"
    ports = svc["spec"]["ports"]
    assert ports == [{"name": "http", "port": 5051, "targetPort": "http"}]


def test_it_enables_prometheus_scraping_of_the_same_http_port():
    annotations = _doc("Deployment")["spec"]["template"]["metadata"]["annotations"]
    assert annotations["prometheus.io/scrape"] == "true"
    assert annotations["prometheus.io/path"] == "/metrics"
    assert annotations["prometheus.io/port"] == "5051"


def test_it_declares_the_expected_environment_durable_outbox_and_required_token_secret():
    dep = _doc("Deployment")
    spec = dep["spec"]["template"]["spec"]
    env = {item["name"]: item for item in spec["containers"][0]["env"]}
    assert env["HEAR_HEARTBEAT_TTL_S"]["value"] == "30"
    assert env["HEAR_HEARTBEAT_SOCKET_TIMEOUT_S"]["value"] == "0.5"
    assert env["REDIS_HOST"]["value"] == "audit-redis.infra.svc.cluster.local"
    assert env["REDIS_PORT"]["value"] == "6379"
    assert env["HEAR_DURABLE_STORE"]["value"] == "sqlite"
    assert env["HEAR_DURABLE_DB"]["value"] == "/state/heartbeat-receiver.sqlite3"
    assert env["HEAR_DURABLE_REPLAY_LIMIT"]["value"] == "256"
    assert env["REDIS_PASS"]["valueFrom"]["secretKeyRef"] == {
        "name": "dama-redis-secret",
        "key": "REDIS_PASS",
        "optional": True,
    }
    assert env["HEAR_HEARTBEAT_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "hear-heartbeat-token",
        "key": "token",
    }
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["state"]["persistentVolumeClaim"] == {"claimName": "hear-heartbeat-state"}


def test_it_declares_a_dedicated_state_pvc_for_rollback_safe_sqlite_storage():
    pvc = _doc("PersistentVolumeClaim")
    assert pvc["metadata"]["name"] == "hear-heartbeat-state"
    assert pvc["metadata"]["namespace"] == "dama"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "5Gi"
