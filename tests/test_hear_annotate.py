"""DAMA Hear tailnet annotation service tests."""

import array
import io
import json
import math
import os
import sqlite3
import struct
import sys
import time
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


# --------------------------------------------------------------------------- queue cost

def _big_pool(tmp_path, n, annotated=()):
    """A corpus the size the live one actually reached, with both tag lanes populated."""
    rows = [_row("k%05d" % i, ts_utc_s=1788987605.0 + i) for i in range(n)]
    tags = [{"clip_key": r["clip_key"], "model": {"name": "yamnet", "version": "1"},
             "scores": [{"label": "Dog", "score": 0.5}, {"label": "Bird", "score": 0.45},
                        {"label": "Cat", "score": 0.3}]} for r in rows]
    birdnet = [{"clip_key": r["clip_key"],
                "scores": [{"label": "Great Horned Owl", "score": 0.6}]} for r in rows]
    pool = _pool(tmp_path, rows, tags=tags, birdnet=birdnet)
    return pool, rows


def _count_parses(monkeypatch):
    """Count JSONL parses -- the O(corpus) work `/api/queue` used to repeat on every request."""
    calls = []
    real = HA._read_jsonl

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(HA, "_read_jsonl", counting)
    return calls


def _reference_queue(pool, db, limit):
    """The queue recomputed from scratch, by a client that has never cached anything."""
    fresh = HA.create_app(str(pool), str(db))
    return TestClient(fresh).get("/api/queue?limit=%d" % limit).json()


def test_queue_parses_the_corpus_once_across_repeated_requests(tmp_path, monkeypatch):
    """⚠️THE DEFECT: every `/api/queue` re-parsed index.jsonl and both tag JSONLs. At 1,878
    clips that was a 1.19 s median against a 1 s readiness timeout, and the corpus only grows."""
    pool, _rows = _big_pool(tmp_path, 50)
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))
    calls = _count_parses(monkeypatch)

    first = client.get("/api/queue?limit=5").json()
    after_first = len(calls)
    for _ in range(9):
        assert client.get("/api/queue?limit=5").json() == first

    assert after_first <= 4, "one cold queue must not parse more than the corpus sources once"
    assert len(calls) == after_first, (
        "nine further queue requests re-parsed %d JSONL files; the corpus parse must be cached "
        "and invalidated by its sources, not repeated per request" % (len(calls) - after_first))


def test_repeat_queue_requests_stay_far_inside_the_readiness_budget(tmp_path):
    """A warm request must not carry the corpus parse -- that is what took the pod NotReady."""
    pool, _rows = _big_pool(tmp_path, 1500)
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))

    start = time.perf_counter()
    client.get("/api/queue?limit=25")
    cold = time.perf_counter() - start

    warm = []
    for _ in range(10):
        start = time.perf_counter()
        client.get("/api/queue?limit=25")
        warm.append(time.perf_counter() - start)
    median = sorted(warm)[len(warm) // 2]

    assert median < 0.05, "warm queue median %.3f s still pays the corpus parse" % median
    assert median < cold / 3.0, (
        "warm queue median %.3f s is not meaningfully cheaper than the cold %.3f s"
        % (median, cold))


def test_queue_cost_does_not_grow_with_the_corpus_once_warm(tmp_path):
    """The live failure mode was growth: a fix that is merely faster crosses the same cliff."""
    small_pool, _ = _big_pool(tmp_path / "small", 400)
    big_pool, _ = _big_pool(tmp_path / "big", 3200)

    def warm_median(pool, db):
        client = TestClient(HA.create_app(str(pool), str(db)))
        client.get("/api/queue?limit=25")
        times = []
        for _ in range(10):
            start = time.perf_counter()
            client.get("/api/queue?limit=25")
            times.append(time.perf_counter() - start)
        return sorted(times)[len(times) // 2]

    small = warm_median(small_pool, tmp_path / "small.sqlite3")
    big = warm_median(big_pool, tmp_path / "big.sqlite3")
    assert big < max(4.0 * small, 0.05), (
        "an 8x corpus made the warm queue %.4f s against %.4f s -- still O(corpus) per request"
        % (big, small))


def test_cached_queue_is_identical_to_a_freshly_computed_one(tmp_path):
    pool, rows = _big_pool(tmp_path, 60)
    db = tmp_path / "ann.sqlite3"
    client = TestClient(HA.create_app(str(pool), str(db)))

    for _ in range(3):
        assert client.get("/api/queue?limit=25").json() == _reference_queue(pool, db, 25)


def test_annotating_a_clip_drops_it_from_the_very_next_queue(tmp_path):
    """⚠️NO FILE CHANGES WHEN A LABEL IS WRITTEN. A cache keyed only on the corpus files would
    keep serving an already-annotated clip, and two annotators would label the same clip."""
    pool, rows = _big_pool(tmp_path, 40)
    db = tmp_path / "ann.sqlite3"
    client = TestClient(HA.create_app(str(pool), str(db)))

    first = client.get("/api/queue?limit=5").json()["clips"][0]["clip_key"]
    assert client.post("/api/annotations", json={"clip_key": first, "label": "dog"}).status_code == 200

    after = client.get("/api/queue?limit=5").json()
    assert first not in [c["clip_key"] for c in after["clips"]]
    assert after == _reference_queue(pool, db, 5)


def test_a_clip_appended_to_the_index_appears_in_the_next_queue(tmp_path):
    pool, rows = _big_pool(tmp_path, 20)
    db = tmp_path / "ann.sqlite3"
    client = TestClient(HA.create_app(str(pool), str(db)))
    client.get("/api/queue?limit=50")

    fresh = _row("arrived-late", ts_utc_s=1788999999.0)
    with open(pool / "corpus" / "clips" / "index.jsonl", "a") as fh:
        fh.write(json.dumps(fresh) + "\n")

    keys = [c["clip_key"] for c in client.get("/api/queue?limit=50").json()["clips"]]
    assert "arrived-late" in keys
    assert client.get("/api/queue?limit=50").json() == _reference_queue(pool, db, 50)


def test_an_atomically_replaced_tags_file_invalidates_the_cache(tmp_path):
    """The tagger writes tmp-then-rename, and a replacement can land with the byte count and
    even the mtime of the file it replaced. Identity, not size, is what must be compared."""
    pool, rows = _big_pool(tmp_path, 12)
    db = tmp_path / "ann.sqlite3"
    client = TestClient(HA.create_app(str(pool), str(db)))
    before = client.get("/api/queue?limit=12").json()

    tags = pool / "corpus" / "clips" / "tags.jsonl"
    stat = os.stat(tags)
    replacement = [{"clip_key": r["clip_key"], "model": {"name": "yamnet", "version": "1"},
                    "scores": [{"label": "Dog", "score": 0.5}, {"label": "Bird", "score": 0.45},
                               {"label": "Cat", "score": 0.3}]} for r in rows]
    replacement[0]["scores"] = [{"label": "Gunshot", "score": 0.51},
                                {"label": "Silence", "score": 0.50},
                                {"label": "Cat", "score": 0.3}]
    tmp = tags.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in replacement))
    os.replace(tmp, tags)
    os.utime(tags, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    after = client.get("/api/queue?limit=12").json()
    assert after != before, "a replaced tags file was not noticed"
    assert after == _reference_queue(pool, db, 12)


def test_a_truncated_index_empties_the_queue_rather_than_serving_the_old_one(tmp_path):
    pool, _rows = _big_pool(tmp_path, 15)
    db = tmp_path / "ann.sqlite3"
    client = TestClient(HA.create_app(str(pool), str(db)))
    assert client.get("/api/queue?limit=15").json()["count"] == 15

    open(pool / "corpus" / "clips" / "index.jsonl", "w").close()
    assert client.get("/api/queue?limit=15").json() == {"clips": [], "count": 0}


def test_a_missing_corpus_index_is_an_empty_queue_and_a_ready_service(tmp_path):
    pool = tmp_path / "pool"
    (pool / "corpus" / "clips").mkdir(parents=True)
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))
    assert client.get("/api/queue?limit=5").json() == {"clips": [], "count": 0}
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["corpus_index"] is False


def test_cached_index_still_refuses_traversal_and_unlisted_clips(tmp_path):
    rows = [_row("ok"), dict(_row("escape"), path="../../etc/passwd"),
            dict(_row("absolute"), path="/etc/passwd")]
    pool = _pool(tmp_path, rows, audios={"ok": _tone(1000, 45)})
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))

    assert client.get("/api/audio/ok").status_code == 200
    for key in ("escape", "absolute"):
        assert client.get("/api/audio/%s" % key).status_code == 404
        assert client.post("/api/annotations", json={"clip_key": key, "label": "dog"}).status_code == 404
    assert client.get("/api/audio/../../etc/passwd").status_code == 404
    assert [c["clip_key"] for c in client.get("/api/queue?limit=9").json()["clips"]] == ["ok"]
    # repeat: a cached index must not become more permissive on the second request
    assert client.get("/api/audio/escape").status_code == 404


def test_concurrent_queue_reads_and_annotations_stay_consistent(tmp_path):
    """A shared cache is shared state: concurrent readers and writers must not see a torn
    queue, lose an annotation, or hand the same clip to two annotators twice."""
    pool, rows = _big_pool(tmp_path, 300)
    db = tmp_path / "ann.sqlite3"
    client = TestClient(HA.create_app(str(pool), str(db)))
    targets = [c["clip_key"] for c in client.get("/api/queue?limit=40").json()["clips"]]

    def annotate(key):
        return client.post("/api/annotations",
                           json={"clip_key": key, "label": "dog", "submission_id": "sub-%s" % key})

    def read(_i):
        body = client.get("/api/queue?limit=40").json()
        assert body["count"] == len(body["clips"])
        assert len(set(c["clip_key"] for c in body["clips"])) == len(body["clips"])
        return body

    with ThreadPoolExecutor(max_workers=8) as pool_exec:
        writes = [pool_exec.submit(annotate, key) for key in targets]
        writes += [pool_exec.submit(annotate, key) for key in targets]  # duplicate submissions
        reads = [pool_exec.submit(read, i) for i in range(20)]
        assert all(f.result().status_code == 200 for f in writes)
        for f in reads:
            f.result()

    export = client.get("/api/export").json()
    assert export["count"] == len(targets), "idempotent submissions must not duplicate rows"
    assert sorted(r["clip_key"] for r in export["annotations"]) == sorted(targets)
    final = client.get("/api/queue?limit=40").json()
    assert not set(c["clip_key"] for c in final["clips"]) & set(targets)
    assert final == _reference_queue(pool, db, 40)


def test_readiness_endpoint_reports_the_dependencies_without_building_a_queue(tmp_path, monkeypatch):
    """⚠️THE PROBE MUST NOT BE A USER QUERY. `GET /api/queue?limit=1` made readiness cost a
    corpus parse; this endpoint must prove the database and nothing expensive."""
    pool, _rows = _big_pool(tmp_path, 200)
    client = TestClient(HA.create_app(str(pool), str(tmp_path / "ann.sqlite3")))

    calls = _count_parses(monkeypatch)
    monkeypatch.setattr(HA, "build_queue", lambda *a, **k: pytest.fail("readiness built a queue"))

    start = time.perf_counter()
    response = client.get("/healthz")
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "corpus_index": True}
    assert response.headers["cache-control"] == "no-store"
    assert calls == [], "readiness parsed %r" % calls
    assert elapsed < 0.25, "readiness took %.3f s" % elapsed


def test_readiness_fails_when_the_annotation_store_is_unusable(tmp_path):
    """Availability must not be faked: the failure the old probe surfaced still surfaces."""
    pool, _rows = _big_pool(tmp_path, 5)
    db = tmp_path / "ann.sqlite3"
    client = TestClient(HA.create_app(str(pool), str(db)))
    assert client.get("/healthz").status_code == 200

    for suffix in ("-wal", "-shm"):
        (tmp_path / ("ann.sqlite3" + suffix)).unlink(missing_ok=True)
    db.write_bytes(b"this is not a database")
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "unready"
