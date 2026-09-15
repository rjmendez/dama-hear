"""Does the thing the image would run actually come up, on the environment the manifest gives it?

⚠️THIS IS A SMOKE TEST, NOT A UNIT TEST, AND IT TOUCHES NOTHING THAT IS NOT A tmp_path. It does
not build an image, reach a registry, or go near the cluster. What it does is reconstruct the
image's *filesystem contract* -- the exact `COPY` sources at the exact destinations, read out of
`deploy/images/service/Dockerfile.hear-heartbeat` -- and start the exact `ENTRYPOINT` argv read
out of the same file, under the env the proposed cutover manifest declares.

The gap it closes: `tests/test_service_images.py` proves the Dockerfile and the manifest agree
with each other and with the plan, and the images workflow proves the built image contains what
the repo says. Neither of those runs the program the way the pod will. A cutover whose entrypoint
cannot find its own module, or whose `PYTHONPATH` only worked because a ConfigMap mount happened
to shadow a path, fails here instead of on a node that holds `hostPort` 5051.

⚠️REDIS IS DELIBERATELY UNREACHABLE. The receiver commits to its SQLite outbox *first* and
reflects into Redis second; with the cache down, an accepted POST must still be durable. That
ordering is the whole reason this workload has a PVC, and it is the property a packaging change
is most likely to break by accident (a lost env var, a read-only path, a wrong uid). Proving it
offline means the live cutover starts from a known-good expectation rather than a hope.
"""
import json
import os
import pathlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "images" / "service" / "Dockerfile.hear-heartbeat"
PROPOSED = ROOT / "deploy" / "k8s" / "hear-heartbeat.proposed.yaml"

TOKEN = "smoke-token"
HEARTBEAT = {
    "telemetry_path": "hear/heartbeat",
    "telemetry_schema_version": 1,
    "device_id": "nyquist",
    "ts": "2026-09-15T12:00:00Z",
    "class": "xiao-s3-pps",
    "fw_version": "7f84d29",
    "uptime_s": 12345,
    "gps": {"fix": 3},
    "time": {"valid": True},
    "wifi": {"rssi_dbm": -67},
    "counters": {
        "scene_rows_written": 1,
        "dets_rows_written": 2,
        "clips_written": 3,
        "clips_evicted": 0,
    },
}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _image_layout():
    """[(repo-relative source, absolute in-image destination)] from the Dockerfile's COPYs."""
    return [(src, dst) for src, dst in
            re.findall(r"^COPY\s+(\S+)\s+(\S+)", DOCKERFILE.read_text(), re.M)
            if not src.startswith("requirements/")]


def _entrypoint():
    (found,) = re.findall(r"^ENTRYPOINT\s+(\[.*\])\s*$", DOCKERFILE.read_text(), re.M)
    return json.loads(found)


def _manifest_env():
    """The literal env the proposed manifest sets. `valueFrom` entries are the caller's job."""
    doc = next(d for d in yaml.safe_load_all(PROPOSED.read_text())
               if d and d["kind"] == "Deployment")
    container = doc["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container["env"] if "value" in e}


def _get(url, timeout=5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def _post(url, payload, token=TOKEN):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Hear-Token": token}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def receiver(tmp_path):
    """The image's file layout and entrypoint, running under the manifest's env."""
    rootfs = tmp_path / "rootfs"
    for src, dst in _image_layout():
        target = rootfs / dst.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / src, target)

    state = tmp_path / "state"
    state.mkdir()

    env = dict(_manifest_env())
    # /state is the PVC; it keeps its filename, which is what the manifest pins.
    db = state / pathlib.PurePosixPath(env["HEAR_DURABLE_DB"]).name
    env["HEAR_DURABLE_DB"] = str(db)
    # ⚠️Nothing listens here. See the module docstring: the cache being down is the point.
    env["REDIS_HOST"] = "127.0.0.1"
    env["REDIS_PORT"] = str(_free_port())
    # The two secretKeyRef values the cluster injects. The token is required, so a smoke test
    # that did not set it would be testing an unauthenticated receiver.
    env["HEAR_HEARTBEAT_TOKEN"] = TOKEN
    # What deploy/images/Dockerfile.runtime bakes in, and nothing else: no inherited PYTHONPATH,
    # no repository on sys.path. If the entrypoint needs the checkout, it fails here.
    env["PYTHONPATH"] = str(rootfs / "app")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PATH"] = os.environ.get("PATH", "")

    port = _free_port()
    argv = [a if a != "python" else sys.executable for a in _entrypoint()]
    argv = [str(rootfs / a.lstrip("/")) if a.startswith("/app/") else a for a in argv]
    argv[argv.index("--port") + 1] = str(port)
    # Bind the loopback only: this is a test process on a shared machine, and the receiver's
    # default is 0.0.0.0 because in the cluster it is on hostNetwork.
    argv += ["--bind", "127.0.0.1"]

    proc = subprocess.Popen(argv, env=env, cwd=str(rootfs), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % port
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail("the entrypoint exited %s:\n%s"
                            % (proc.returncode, proc.stdout.read()))
            try:
                _get(base + "/healthz", timeout=1.0)
                break
            except Exception:
                time.sleep(0.2)
        else:
            proc.kill()
            pytest.fail("the entrypoint never served /healthz:\n%s" % proc.stdout.read())
        yield base, db, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - only if it hangs on shutdown
            proc.kill()


def test_the_image_entrypoint_serves_the_probe_the_manifest_polls(receiver):
    """The readiness and liveness probes are `GET /healthz`; nothing else decides `Ready`."""
    base, _db, _proc = receiver
    status, body = _get(base + "/healthz")
    assert status == 200
    assert body["status"] == "ok"
    assert body["service"] == "hear-heartbeat-receiver"
    # The shape the operator compares before and after the cutover, per gate 1.
    assert set(body) == {"status", "service", "redis_target", "durable_store", "refusals"}
    assert body["durable_store"]["backend"] == "sqlite"


def test_an_accepted_post_is_durable_before_it_is_cached(receiver):
    """⚠️THE PROPERTY THE PVC EXISTS FOR, PROVEN THROUGH THE ENTRYPOINT.

    Redis is unreachable, so the POST reports 503 -- the caller is told the cache write failed.
    The record is committed regardless, with a failed cache attempt recorded against it, which
    is what the replay worker later repairs. A packaging change that lost the state mount, the
    `HEAR_DURABLE_*` env or the ability to write `/state` would produce a durable row count of
    zero here while `/healthz` stayed green.
    """
    base, db, _proc = receiver
    status, _body = _post(base + "/api/hear/heartbeat", HEARTBEAT)
    assert status == 503, "with Redis down the cache write must be reported as failed"

    con = sqlite3.connect(db)
    try:
        records = con.execute(
            "SELECT telemetry_path, device_id FROM durable_records").fetchall()
        outcomes = [r[0] for r in con.execute("SELECT outcome FROM cache_attempts").fetchall()]
    finally:
        con.close()
    assert records == [("hear/heartbeat", "nyquist")]
    assert outcomes and all(o != "success" for o in outcomes), outcomes


def test_the_same_record_twice_stays_one_durable_row(receiver):
    """A node that retries after a 503 must not multiply into the ledger."""
    base, db, _proc = receiver
    for _ in range(3):
        _post(base + "/api/hear/heartbeat", HEARTBEAT)
    con = sqlite3.connect(db)
    try:
        (count,) = con.execute("SELECT count(*) FROM durable_records").fetchone()
    finally:
        con.close()
    assert count == 1


def test_the_token_secret_is_still_enforced(receiver):
    """HEAR_HEARTBEAT_TOKEN comes from a Secret and the cutover does not touch it."""
    base, _db, _proc = receiver
    status, _body = _post(base + "/api/hear/heartbeat", HEARTBEAT, token="wrong")
    assert status == 401


def test_nothing_in_the_image_layout_needs_the_checkout(receiver):
    """PYTHONPATH is /app and the repository is not on it: the image is self-contained."""
    _base, _db, proc = receiver
    assert proc.poll() is None
