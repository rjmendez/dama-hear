"""The Perch 2.0 lane: registry wiring, pinned files, input preparation, embed-only rows and
gates, and the GPU-pinned manifest that ships suspended. TensorFlow is never imported here."""
import hashlib
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_hear_tag import (GC, HT, ROOT, TAGS, VERIFIED, quiet_noise,  # noqa: E402
                           store_clip)

EMBED_MANIFEST = os.path.join(ROOT, "deploy", "k8s", "hear-embed.yaml")
PV = {"ok": True, "model_sha256": "ab" * 32, "model_bytes": 1, "files": {}, "problems": []}


class StubEmbedder:
    def __init__(self):
        self.calls = []

    def tag(self, pcm, floor=0.0):
        self.calls.append(np.asarray(pcm))
        return {"scores": None, "max_unstored_score": None, "n_classes_scored": 0,
                "n_passes": 1, "embedding": [0.0] * HT.PERCH_EMBED_DIM,
                "embedding_dim": HT.PERCH_EMBED_DIM}


def run_perch(root, emb, **kw):
    return HT.run(str(root), model_dir="/nonexistent", tagger=emb, verified=PV,
                  lane="perch_v2", **kw)


class TestTheRegistry:

    def test_every_lane_names_a_known_model(self):
        for lane, spec in HT.LANES.items():
            assert spec["model"] in HT.MODELS, lane

    def test_mn10_rows_keep_their_identity(self):
        mb = HT.model_block(VERIFIED)
        assert (mb["name"], mb["version"]) == (HT.MODEL_NAME, HT.MODEL_VERSION)

    def test_perch_writes_its_own_store_and_card(self, tmp_path):
        store_clip(tmp_path)
        run_perch(tmp_path, StubEmbedder())
        rows = list(TAGS.read_tags(str(tmp_path), "perch_v2"))
        assert len(rows) == 1 and not list(TAGS.read_tags(str(tmp_path)))
        assert os.path.exists(HT.card_path(str(tmp_path), "tag_model_card-perch_v2.json"))
        r = rows[0]
        assert (r["model"]["name"], r["lane"]) == ("perch_v2", "perch_v2")
        assert r["scores"] is None and r["embedding_dim"] == 1536
        assert r["model"]["head_stored"] is False
        assert r["tag_key"] == TAGS.tag_key(r["clip_key"], "perch_v2", HT.PERCH_VERSION,
                                            PV["model_sha256"])


class TestPerchInput:

    def test_exactly_five_seconds_at_32k_peak_normalised(self, tmp_path):
        store_clip(tmp_path)
        e = StubEmbedder()
        run_perch(tmp_path, e)
        x = e.calls[0]
        assert len(x) == HT.PERCH_WINDOW
        assert abs(float(np.max(np.abs(x))) - HT.PERCH_TARGET_PEAK) < 1e-6

    def test_a_recovered_header_clip_reaches_perch(self, tmp_path):
        store_clip(tmp_path, pcm=quiet_noise(n=240000), fs=16000, fs_csv=16000.0)
        e = StubEmbedder()
        t = run_perch(tmp_path, e)
        assert t["tagged"] == 1 and len(e.calls[0]) == HT.PERCH_WINDOW

    def test_digital_silence_is_refused_not_scaled(self, tmp_path):
        store_clip(tmp_path, pcm=np.zeros(240000))
        t = run_perch(tmp_path, StubEmbedder())
        assert t["by_reason"] == {HT.R_DIGITAL_SILENCE: 1}


class TestEmbedOnlyGates:

    def test_no_scores_is_not_read_as_a_dead_model(self, tmp_path):
        store_clip(tmp_path)
        t = run_perch(tmp_path, StubEmbedder())
        assert t["scored_any"] is None and t["mean_top_score"] is None and t["heard"] == {}
        code, lines = HT.check_tags(str(tmp_path), lane="perch_v2", now=t["at"])
        assert not any("NONE" in l for l in lines), lines
        assert code == 0, lines

    def test_one_invocation_runs_one_model(self, tmp_path):
        assert HT.main(["--pool", str(tmp_path), "--model-dir", str(tmp_path),
                        "--lane", "mn10", "--lane", "perch_v2"]) == 2

    def test_verify_weights_follows_the_lanes_model(self, tmp_path, capsys):
        assert HT.main(["--lane", "perch_v2", "--model-dir", str(tmp_path),
                        "--verify-weights"]) == 2
        assert "saved_model.pb" in capsys.readouterr().err


class TestThePinnedFiles:

    def _stage(self, d, monkeypatch):
        files = []
        for i, rel in enumerate(["saved_model.pb", "variables/variables.index"]):
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            body = ("file %d" % i).encode()
            p.write_bytes(body)
            files.append((rel, hashlib.sha256(body).hexdigest(), len(body)))
        monkeypatch.setattr(HT, "PERCH_FILES", tuple(files))

    def test_all_present_verifies_and_names_a_tree_digest(self, tmp_path, monkeypatch):
        self._stage(tmp_path, monkeypatch)
        v = HT.verify_perch(str(tmp_path))
        assert v["ok"] and len(v["model_sha256"]) == 64

    def test_a_changed_file_is_refused(self, tmp_path, monkeypatch):
        self._stage(tmp_path, monkeypatch)
        (tmp_path / "saved_model.pb").write_bytes(b"file X")
        v = HT.verify_perch(str(tmp_path))
        assert not v["ok"] and v["model_sha256"] is None
        assert "saved_model.pb" in v["problems"][0]

    def test_a_missing_file_names_the_archive(self, tmp_path, monkeypatch):
        self._stage(tmp_path, monkeypatch)
        (tmp_path / "saved_model.pb").unlink()
        v = HT.verify_perch(str(tmp_path))
        assert not v["ok"] and HT.PERCH_ARCHIVE_SHA256 in v["problems"][0]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_an_unreadable_file_is_a_problem_not_a_crash(self, tmp_path, monkeypatch):
        self._stage(tmp_path, monkeypatch)
        (tmp_path / "saved_model.pb").chmod(0)
        try:
            v = HT.verify_perch(str(tmp_path))
        finally:
            (tmp_path / "saved_model.pb").chmod(0o644)
        assert not v["ok"] and "could not be read" in v["problems"][0]

    def test_the_pins_cover_the_whole_measured_savedmodel(self):
        assert len(HT.PERCH_FILES) == 6
        assert sum(n for _r, _s, n in HT.PERCH_FILES) == 410275841


@pytest.fixture(scope="module")
def edocs():
    with open(EMBED_MANIFEST) as fh:
        return [d for d in yaml.safe_load_all(fh) if d]


def _container(doc):
    return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]


class TestTheEmbedManifest:

    def test_both_cronjobs_ship_enabled(self, edocs):
        """Suspended until the model was staged and the GPU path proven in-cluster (2026-09-11);
        enabled in the repo so a later apply cannot quietly switch the lane off."""
        assert [d["metadata"]["name"] for d in edocs] == ["hear-embed", "hear-embed-check"]
        assert all(d["spec"]["suspend"] is False for d in edocs)

    def test_the_embed_job_is_pinned_to_the_2080ti(self, edocs):
        c = _container(edocs[0])
        env = {e["name"]: e["value"] for e in c["env"]}
        assert env["CUDA_VISIBLE_DEVICES"] == "0" and env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
        assert str(c["resources"]["limits"]["nvidia.com/gpu"]) == "1"
        assert c["image"] == "tensorflow/tensorflow:2.20.0-gpu"
        assert "--lane perch_v2" in c["args"][0]

    def test_the_check_needs_no_gpu(self, edocs):
        assert "nvidia.com/gpu" not in str(_container(edocs[1]).get("resources"))

    def test_every_bundle_key_is_mounted(self, edocs):
        keys = {k for k, _rel in GC.BUNDLES["hear-tag-code"][1]}
        for d in edocs:
            got = {m["subPath"] for m in _container(d)["volumeMounts"] if m.get("subPath")}
            assert got == keys, d["metadata"]["name"]
            vols = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
            assert any((v.get("configMap") or {}).get("name") == "hear-tag-code" for v in vols)

    def test_it_never_reaches_a_node(self):
        with open(EMBED_MANIFEST) as fh:
            body = "\n".join(l for l in fh.read().splitlines() if not l.strip().startswith("#"))
        assert "172.16.100." not in body

    def test_a_failing_check_fails_the_job(self, edocs, tmp_path):
        stub = tmp_path / "python"
        stub.write_text("#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = \"--check\" ] && exit 1; "
                        "done\nexit 7\n")
        stub.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (tmp_path, os.environ.get("PATH", "")))
        r = subprocess.run(["/bin/sh", "-lc", textwrap.dedent(_container(edocs[1])["args"][0])],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 1
