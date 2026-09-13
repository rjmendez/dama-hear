import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import pool as P                                          # noqa: E402
from hear import scenefile as SF                                    # noqa: E402
from tools import hear_drain as HD                                  # noqa: E402


def _mel_hex(bands=20, slices=4, seed=1):
    rng = np.random.default_rng(seed)
    q = rng.integers(-128, 127, size=(bands, slices), dtype=np.int8)
    return q.tobytes().hex()


def _scene_row(gen, utc_us, uptime_s, sample, node="nyquist", seed=1):
    vals = {
        "node": node,
        "utc_us": str(utc_us),
        "uptime_s": str(uptime_s),
        "sample": str(sample),
        "bands": "20",
        "slices": "4",
        "span_ms": "1024",
        "ref_db4": "251",
        "frames": "64",
        "fft_us": "18987",
        "mel_hex": _mel_hex(seed=seed),
        "f_lo_hz": "62.5",
        "f_hi_hz": "7812.5",
    }
    return ",".join(vals[c] for c in gen.written)


def _scene_csv(rows=3, node="nyquist", start=1_788_813_441_000_000, gen=SF.S2):
    out = [",".join(gen.declared)]
    for i in range(rows):
        out.append(_scene_row(gen, start + i * 1_024_000, 100 + i, 16_384 * (i + 1), node=node,
                              seed=i + 1))
    return ("\n".join(out) + "\n").encode()


class SceneNode:
    def __init__(self, node="nyquist", sizes=None, bodies=None):
        self.node = node
        self.sizes = dict(sizes or {})
        self.bodies = dict(bodies or {})
        self.sd_calls = []

    def status(self, ip, timeout=None):
        return {
            "node": self.node,
            "uptime_s": 54912,
            "scene": {"rows": 3, "written": 3, "short_blocks": 0, "write_fail": 0},
            "acq": {"fs_clean_hz": 16000.0, "win_s": 300, "drop_s": 0, "drop_samples": 0},
        }

    def ls(self, ip, timeout=None):
        return dict(self.sizes)

    def sd(self, ip, name, timeout=None, tail=None):
        self.sd_calls.append((name, tail))
        body = self.bodies.get(name)
        if body is None:
            return None
        if tail and len(body) > tail:
            body = body[len(body) - tail:]
            return body if b"\n" in body else None
        return body


@pytest.fixture
def wired(monkeypatch):
    def build(**kw):
        node = SceneNode(**kw)
        monkeypatch.setattr(HD, "fetch_status", node.status)
        monkeypatch.setattr(HD, "fetch_sd", node.sd)
        monkeypatch.setattr(HD, "_ls_sizes", node.ls)
        return node
    return build


def _pool(tmp_path):
    return P.Pool(str(tmp_path / "pool"))


class TestSceneFileSelection:
    def test_dated_files_are_preferred_over_legacy(self, tmp_path, wired, monkeypatch):
        monkeypatch.setattr(HD, "SCENE_TAIL_BYTES", 1_000_000)
        bodies = {
            "scene.csv": _scene_csv(node="nyquist", start=1_788_700_000_000_000),
            "scene-prev.csv": _scene_csv(node="nyquist", start=1_788_600_000_000_000),
            "scene-20260911.csv": _scene_csv(node="nyquist", start=1_788_800_000_000_000),
            "scene-20260912-prev.csv": _scene_csv(node="nyquist", start=1_788_810_000_000_000),
            "scene-20260912.csv": _scene_csv(node="nyquist", start=1_788_820_000_000_000),
        }
        sizes = {name: len(body) for name, body in bodies.items()}
        node = wired(sizes=sizes, bodies=bodies)

        result = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")

        scene_calls = [call for call in node.sd_calls if call[0].startswith("scene")]
        assert scene_calls == [
            ("scene-20260911.csv", None),
            ("scene-20260912-prev.csv", None),
            ("scene-20260912.csv", 1_000_000),
        ]
        assert result["scene_live"] == "scene-20260912.csv"
        assert result["scene_files"] == [
            "scene-20260911.csv",
            "scene-20260912-prev.csv",
            "scene-20260912.csv",
        ]
        assert result["scene_added"] > 0

    def test_scene_names_pick_the_newest_dated_file_by_date(self):
        sizes = {
            "scene-20261231.csv": 1,
            "scene-20270101-prev.csv": 1,
            "scene-20270101.csv": 1,
            "scene-00000000.csv": 1,
        }
        names, live = HD.scene_names(sizes)
        assert names == (
            "scene-00000000.csv",
            "scene-20261231.csv",
            "scene-20270101-prev.csv",
            "scene-20270101.csv",
        )
        assert live == "scene-20270101.csv"

    def test_legacy_only_fallback_still_works(self, tmp_path, wired, monkeypatch):
        monkeypatch.setattr(HD, "SCENE_TAIL_BYTES", 1_000_000)
        body = _scene_csv(node="nyquist")
        node = wired(sizes={"scene.csv": len(body)}, bodies={"scene.csv": body})

        result = HD.drain_node(_pool(tmp_path), "nyquist", "10.0.0.1")

        scene_calls = [call for call in node.sd_calls if call[0].startswith("scene")]
        assert scene_calls == [("scene.csv", 1_000_000), ("scene-prev.csv", None)]
        assert result["scene_live"] == "scene.csv"
        assert result["scene_added"] > 0
        assert result["scene_missing"] is False


class TestSceneCheckFailures:
    def test_missing_every_scene_file_fails_the_check(self, tmp_path, wired):
        pl = _pool(tmp_path)
        node = wired(node="rankine", sizes={"health.csv": 100, "dets.csv": 50}, bodies={})

        result = HD.drain_node(pl, "rankine", "10.0.0.3")
        HD.write_heartbeat(pl.root, [result], None, now=1000.0)
        code, lines = HD.check(pl.root, max_stale_s=7200.0, now=1010.0)

        assert result["scene_files"] == []
        assert result["scene_live"] is None
        assert result["scene_missing"] is True
        assert result["unfetched_unknown"] is False
        assert result["unfetched_reason"] == "the node served no scene file of any name"
        assert code == 1
        assert len(lines) == 1
        assert "NO SCENE FILE" in lines[0]
        assert "UNKNOWN" not in lines[0]
        assert [call for call in node.sd_calls if call[0].startswith("scene")] == []
