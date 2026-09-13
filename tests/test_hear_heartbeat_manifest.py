"""The heartbeat receiver manifest: always on, service-backed, and wired to the new entrypoint."""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "k8s" / "hear-heartbeat.yaml"


def _docs():
    return list(yaml.safe_load_all(MANIFEST.read_text()))


def test_it_declares_one_deployment_and_one_service():
    docs = _docs()
    kinds = [d["kind"] for d in docs]
    assert kinds == ["Deployment", "Service"]


def test_the_receiver_runs_the_new_entrypoint_and_mounts_its_bundle():
    dep = _docs()[0]
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["name"] == "receiver"
    assert c["image"] == "python:3.13-slim"
    assert "hear_heartbeat_receiver.py --port 5051" in c["args"][0]
    mounts = {m["subPath"]: m["mountPath"] for m in c["volumeMounts"] if m["name"] == "code"}
    assert mounts == {"tools_hear_heartbeat_receiver.py": "/app/tools/hear_heartbeat_receiver.py"}


def test_it_exposes_the_http_port_and_health_probe():
    dep, svc = _docs()
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["ports"] == [{"name": "http", "containerPort": 5051}]
    for probe_name in ("livenessProbe", "readinessProbe"):
        probe = c[probe_name]
        assert probe["httpGet"]["path"] == "/healthz"
        assert probe["httpGet"]["port"] == "http"
    ports = svc["spec"]["ports"]
    assert ports == [{"name": "http", "port": 5051, "targetPort": "http"}]


def test_it_declares_the_expected_redis_environment_variables():
    dep = _docs()[0]
    env = {item["name"]: item for item in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HEAR_HEARTBEAT_TTL_S"]["value"] == "30"
    assert env["REDIS_HOST"]["value"] == "100.73.200.19"
    assert env["REDIS_PORT"]["value"] == "30379"
    assert env["REDIS_PASS"]["valueFrom"]["secretKeyRef"]["key"] == "password"
