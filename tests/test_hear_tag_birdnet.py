"""The BirdNET V2.4 lane: registry, the site from the environment only, the week, the species
claim, the range filter recorded on each row, 48 kHz in untouched, pinned files, and the
manifest. LiteRT is never imported here."""
import calendar
import hashlib
import os
import re
import subprocess
import sys
import textwrap

import numpy as np
import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_hear_tag import GC, HT, ROOT, TAGS, store_clip  # noqa: E402

BIRD_MANIFEST = os.path.join(ROOT, "deploy", "k8s", "hear-birdnet.yaml")
BV = {"ok": True, "model_sha256": "cd" * 32, "model_bytes": 1, "files": {}, "problems": []}


class StubBird:
    wants_time = True

    def __init__(self):
        self.calls = []

    def tag(self, pcm, floor=0.0, week=-1):
        self.calls.append((np.asarray(pcm), week))
        return {"scores": {"Cardinalis cardinalis_Northern Cardinal": 0.81, "Dog_Dog": 0.2},
                "max_unstored_score": 0.005, "n_classes_scored": 154, "n_passes": 3,
                "embedding": None, "embedding_dim": None,
                "extra": {"location_filter": {"lat": 40.3, "lon": -76.1, "week": week,
                                              "threshold": 0.03, "species_kept": 154},
                          "max_out_of_range_score": 0.4}}


def run_bird(root, tagger, **kw):
    return HT.run(str(root), model_dir="/nonexistent", tagger=tagger, verified=BV,
                  lane="birdnet_v24", **kw)


def _utc(y, m, d):
    return calendar.timegm((y, m, d, 12, 0, 0, 0, 0, 0))


class TestTheLane:

    def test_it_is_registered_at_48k(self):
        assert HT.LANES["birdnet_v24"]["model"] == "birdnet_v24"
        assert HT.MODELS["birdnet_v24"]["fs_hz"] == 48000

    def test_48k_audio_reaches_birdnet_untouched(self, tmp_path):
        row = store_clip(tmp_path)
        t = StubBird()
        run_bird(tmp_path, t)
        x, _week = t.calls[0]
        pcm, _fs = HT.read_wav(os.path.join(str(tmp_path), row["path"]))
        assert len(x) == 240000 and np.array_equal(x, pcm)

    def test_the_row_carries_the_species_claim_and_the_range_filter(self, tmp_path):
        store_clip(tmp_path)
        run_bird(tmp_path, StubBird())
        r = next(TAGS.read_tags(str(tmp_path), "birdnet_v24"))
        assert r["claim"]["is_species_id"] is True and r["claim"]["human_verified"] is False
        assert r["location_filter"]["week"] == 34 and r["location_filter"]["threshold"] == 0.03
        assert r["max_out_of_range_score"] == 0.4 and r["model"]["licence"] == "CC BY-NC-SA 4.0"
        assert not list(TAGS.read_tags(str(tmp_path)))

    def test_an_unanchored_clip_is_filtered_for_any_week(self, tmp_path):
        store_clip(tmp_path, anchored=False)
        t = StubBird()
        run_bird(tmp_path, t)
        assert t.calls[0][1] == -1

    def test_the_heard_summary_counts_species_at_the_report_score(self, tmp_path):
        store_clip(tmp_path)
        t = run_bird(tmp_path, StubBird())
        assert t["heard"] == {"nyquist": {"Northern Cardinal": 1}}

    def test_other_models_still_claim_no_species(self):
        assert HT.claim_block()["is_species_id"] is False


class TestTheSiteNeverComesFromTheRepo:

    def test_no_site_refuses(self, monkeypatch):
        monkeypatch.delenv(HT.SITE_ENV, raising=False)
        with pytest.raises(HT.WeightsRefused):
            HT.site_latlon()

    def test_a_site_is_rounded_to_a_tenth_of_a_degree(self, monkeypatch):
        monkeypatch.setenv(HT.SITE_ENV, "40.2925,-76.1221")
        assert HT.site_latlon() == (40.3, -76.1)

    @pytest.mark.parametrize("bad", ["40.3", "north,west", "91,0", "0,181"])
    def test_a_bad_site_refuses(self, monkeypatch, bad):
        monkeypatch.setenv(HT.SITE_ENV, bad)
        with pytest.raises(HT.WeightsRefused):
            HT.site_latlon()


class TestTheWeekAndTheGroup:

    @pytest.mark.parametrize("ymd,week", [((2026, 1, 1), 1), ((2026, 9, 11), 34),
                                          ((2026, 9, 30), 36), ((2026, 12, 31), 48)])
    def test_birdnets_48_week_year(self, ymd, week):
        assert HT.birdnet_week(_utc(*ymd)) == week

    def test_no_time_is_any_week(self):
        assert HT.birdnet_week(None) == -1

    def test_the_group_is_the_common_name_or_below_the_report_score(self):
        assert HT.birdnet_group({"Cyanocitta cristata_Blue Jay": 0.7, "Dog_Dog": 0.1}) == "Blue Jay"
        assert HT.birdnet_group({"Cyanocitta cristata_Blue Jay": 0.2}) == "below 0.5"
        assert HT.birdnet_group({}) == "none"


class TestThePinnedFiles:

    def test_staged_files_verify_and_a_missing_one_names_the_archive(self, tmp_path, monkeypatch):
        files = []
        for i, rel in enumerate(["audio-model.tflite", "labels/en_us.txt"]):
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(("b %d" % i).encode())
            files.append((rel, hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size))
        monkeypatch.setattr(HT, "BIRDNET_FILES", tuple(files))
        assert HT.verify_birdnet(str(tmp_path))["ok"]
        (tmp_path / "audio-model.tflite").unlink()
        v = HT.verify_birdnet(str(tmp_path))
        assert not v["ok"] and HT.BIRDNET_ARCHIVE_SHA256 in v["problems"][0]

    def test_the_pins_are_the_measured_archive(self):
        assert [r for r, _s, _n in HT.BIRDNET_FILES] == [
            "audio-model.tflite", "meta-model.tflite", "labels/en_us.txt"]
        assert sum(n for _r, _s, n in HT.BIRDNET_FILES) == 81512248


@pytest.fixture(scope="module")
def bdocs():
    with open(BIRD_MANIFEST) as fh:
        return [d for d in yaml.safe_load_all(fh) if d]


def _container(doc):
    return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]


class TestTheBirdnetManifest:

    def test_both_cronjobs_ship_enabled(self, bdocs):
        assert [d["metadata"]["name"] for d in bdocs] == ["hear-birdnet", "hear-birdnet-check"]
        assert all(d["spec"]["suspend"] is False for d in bdocs)

    def test_the_site_comes_from_the_secret(self, bdocs):
        env = {e["name"]: e for e in _container(bdocs[0])["env"]}
        ref = env[HT.SITE_ENV]["valueFrom"]["secretKeyRef"]
        assert (ref["name"], ref["key"]) == ("hear-site", "latlon")
        assert "value" not in env[HT.SITE_ENV]

    def test_no_coordinates_are_written_into_this_public_file(self):
        with open(BIRD_MANIFEST) as fh:
            text = fh.read()
        assert not re.search(r"-?\d{1,3}\.\d+\s*,\s*-?\d{1,3}\.\d+", text), (
            "the repo is public: the site location belongs in the hear-site Secret")

    def test_it_runs_on_the_cpu_with_a_pinned_runtime(self, bdocs):
        c = _container(bdocs[0])
        assert "nvidia.com/gpu" not in str(c.get("resources"))
        assert c["image"] == "python:3.13-slim"
        assert re.search(r"ai-edge-litert==\d", c["args"][0])
        assert "--lane birdnet_v24" in c["args"][0]

    def test_every_bundle_key_is_mounted(self, bdocs):
        keys = {k for k, _rel in GC.BUNDLES["hear-tag-code"][1]}
        for d in bdocs:
            got = {m["subPath"] for m in _container(d)["volumeMounts"] if m.get("subPath")}
            assert got == keys, d["metadata"]["name"]

    def test_it_never_reaches_a_node(self):
        with open(BIRD_MANIFEST) as fh:
            body = "\n".join(l for l in fh.read().splitlines() if not l.strip().startswith("#"))
        assert "172.16.100." not in body

    def test_a_failing_check_fails_the_job(self, bdocs, tmp_path):
        stub = tmp_path / "python"
        stub.write_text("#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = \"--check\" ] && exit 1; "
                        "done\nexit 7\n")
        stub.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (tmp_path, os.environ.get("PATH", "")))
        r = subprocess.run(["/bin/sh", "-lc", textwrap.dedent(_container(bdocs[1])["args"][0])],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 1
