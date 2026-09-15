"""DAMA Hear tailnet annotation service tests."""

import array
import io
import json
import math
import os
import sqlite3
import struct
import sys
import wave
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "hear_annotate"))

import server as HA  # noqa: E402


def _tone(n, amp, fs=48000, f=440.0):
    return [int(round(amp * math.sin(2 * math.pi * f * i / fs))) for i in range(n)]


def _wav_bytes(samples, fs=48000):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return b.getvalue()


def _read_wav_samples(body):
    with wave.open(io.BytesIO(body), "rb") as w:
        raw = w.readframes(w.getnframes())
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _dbfs_samples(samples):
    rms = math.sqrt(sum(int(s) * int(s) for s in samples) / len(samples))
    return HA.dbfs(rms)


def _pool(tmp_path, rows, audios=None, tags=None, birdnet=None):
    root = tmp_path / "pool" / "corpus"
    clips = root / "clips"
    clips.mkdir(parents=True)
    with open(clips / "index.jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    for key, samples in (audios or {}).items():
        row = next(r for r in rows if r["clip_key"] == key)
        path = root / row["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_wav_bytes(samples))
    for name, data in (("tags.jsonl", tags or []), ("tags-birdnet_v24.jsonl", birdnet or [])):
        with open(clips / name, "w") as f:
            for row in data:
                f.write(json.dumps(row) + "\n")
    return tmp_path / "pool"


def _row(key, node="mach", day="2026-09-10", **kw):
    row = {"outcome": "stored", "clip_key": key, "node": node,
           "path": f"clips/{day}/{node}/{key}.wav", "ts_utc_s": 1788987605.0,
           "anchored": True, "dur_s": 5.0}
    row.update(kw)
    return row


def test_gain_normalization_makes_inaudible_clip_audible_without_clipping():
    quiet = _tone(48000, 45)
    body, meta = HA.normalize_wav_bytes(_wav_bytes(quiet))
    samples = _read_wav_samples(body)
    assert meta["gain_bound_by"] == "rms"
    assert -21.0 <= _dbfs_samples(samples) <= -19.0
    assert max(abs(int(s)) for s in samples) < 32767


def test_gain_normalization_peak_bounds_impulsive_audio():
    samples = [0] * 48000
    for i in range(1000, 1020):
        samples[i] = 30000 if i % 2 else -30000
    body, meta = HA.normalize_wav_bytes(_wav_bytes(samples))
    out = _read_wav_samples(body)
    assert meta["gain_bound_by"] == "peak"
    assert max(abs(int(s)) for s in out) <= 32767


def test_database_persistence_is_append_only_with_human_identity(tmp_path):
    db = tmp_path / "ann.sqlite3"
    st = HA.AnnotateStore(str(db))
    st.append(HA.AnnotationIn(clip_key="a", label="dog", confidence=0.9, notes="bark"), "alice@example")
    st.append(HA.AnnotationIn(clip_key="a", label="ambiguous", confidence=0.4, notes="far"), "bob@example")
    rows = st.export_rows()
    assert [r["label"] for r in rows] == ["dog", "ambiguous"]
    assert all(r["provenance"] == "human" for r in rows)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM annotations WHERE clip_key='a'").fetchone()[0] == 2


def test_queue_prioritizes_unannotated_model_disagreement(tmp_path):
    pool = _pool(
        tmp_path,
        [_row("disagree"), _row("plain"), _row("done")],
        tags=[
            {"clip_key": "disagree", "scores": [{"label": "Dog", "score": 0.55}, {"label": "Bird", "score": 0.45}]},
            {"clip_key": "plain", "scores": [{"label": "Silence", "score": 0.99}]},
            {"clip_key": "done", "scores": [{"label": "Dog", "score": 0.9}]},
        ],
        birdnet=[{"clip_key": "disagree", "scores": [{"label": "Great Horned Owl", "score": 0.6}]}],
    )
    app = HA.create_app(str(pool), str(tmp_path / "ann.sqlite3"))
    client = TestClient(app)
    assert client.post("/api/annotations", json={"clip_key": "done", "label": "dog"}).status_code == 200
    data = client.get("/api/queue?limit=10").json()["clips"]
    assert [c["clip_key"] for c in data] == ["disagree", "plain"]
    assert data[0]["tags"]["audioset"][0]["provenance"] == "model"
    assert "birdnet" in data[0]["tags"]


def test_audio_endpoint_streams_normalized_wav(tmp_path):
    pool = _pool(tmp_path, [_row("a")], audios={"a": _tone(48000, 45)})
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))
    r = client.get("/api/audio/a")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert float(r.headers["x-gain-db"]) > 20.0
    assert -21.0 <= _dbfs_samples(_read_wav_samples(r.content)) <= -19.0


def test_export_endpoint_returns_only_human_ground_truth_and_no_model_tags(tmp_path):
    pool = _pool(
        tmp_path,
        [_row("a")],
        tags=[{"clip_key": "a", "scores": [{"label": "Vehicle", "score": 0.99}], "provenance": "model"}],
    )
    client = TestClient(HA.create_app(
        str(pool), str(tmp_path / "ann.sqlite3"), trusted_proxies=["testclient"]
    ))
    client.post("/api/annotations", json={"clip_key": "a", "label": "dog", "confidence": 0.8, "notes": "human heard bark"},
                headers={"Tailscale-User-Login": "alice@example"})
    data = client.get("/api/export").json()
    assert data["provenance"] == "human"
    assert data["count"] == 1
    assert data["annotations"][0]["label"] == "dog"
    assert data["annotations"][0]["user_id"] == "alice@example"
    assert "Vehicle" not in json.dumps(data)


def test_safe_join_rejects_parent_and_symlink_escape(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    for rel in ("../secret.wav", "link/secret.wav"):
        with pytest.raises(ValueError):
            HA.safe_join(str(root), rel)


def test_audio_endpoint_rejects_empty_wav(tmp_path):
    pool = _pool(tmp_path, [_row("empty")], audios={"empty": []})
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))
    response = client.get("/api/audio/empty")
    assert response.status_code == 422
    assert "no audio frames" in response.json()["detail"]


def test_annotation_validation_rejects_blank_fields_and_oversized_notes(tmp_path):
    pool = _pool(tmp_path, [_row("a")])
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))
    assert client.post("/api/annotations", json={"clip_key": "a", "label": "   "}).status_code == 422
    response = client.post(
        "/api/annotations",
        json={"clip_key": "a", "label": "dog", "notes": "x" * 4097},
    )
    assert response.status_code == 422


def test_submission_id_is_idempotent_across_concurrent_store_instances(tmp_path):
    db = tmp_path / "ann.sqlite3"
    first = HA.AnnotateStore(str(db))
    second = HA.AnnotateStore(str(db))
    ann = HA.AnnotationIn(clip_key="a", label="dog", submission_id="mobile-request-1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(executor.map(lambda store: store.append(ann, "alice"), (first, second)))

    assert rows[0]["id"] == rows[1]["id"]
    assert len(first.export_rows()) == 1


def test_cors_preflight_and_audio_headers_for_configured_mobile_origin(tmp_path):
    pool = _pool(tmp_path, [_row("a")], audios={"a": _tone(1000, 45)})
    client = TestClient(HA.create_app(
        str(pool), str(tmp_path / "ann.sqlite3"), cors_origins=["https://mobile.example"]
    ))
    preflight = client.options(
        "/api/annotations",
        headers={
            "Origin": "https://mobile.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://mobile.example"
    assert "content-type" in preflight.headers["access-control-allow-headers"].lower()

    audio = client.get("/api/audio/a", headers={"Origin": "https://mobile.example"})
    assert audio.headers["access-control-allow-origin"] == "https://mobile.example"
    exposed = audio.headers["access-control-expose-headers"].lower()
    assert "x-gain-db" in exposed and "x-gain-bound-by" in exposed


def test_untrusted_client_cannot_spoof_proxy_identity_header(tmp_path):
    pool = _pool(tmp_path, [_row("a")])
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))
    response = client.post(
        "/api/annotations",
        json={"clip_key": "a", "label": "dog"},
        headers={"Tailscale-User-Login": "victim@example"},
    )
    assert response.status_code == 200
    assert response.json()["user_id"] == "client:testclient"


def test_reused_submission_id_with_different_payload_is_conflict(tmp_path):
    pool = _pool(tmp_path, [_row("a")])
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))
    first = client.post(
        "/api/annotations",
        json={"clip_key": "a", "label": "dog", "submission_id": "same-request"},
    )
    second = client.post(
        "/api/annotations",
        json={"clip_key": "a", "label": "bird", "submission_id": "same-request"},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert client.get("/api/export").json()["count"] == 1
