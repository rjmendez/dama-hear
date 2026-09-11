"""hear-embed brings its own CUDA: the stock TF 2.20 GPU image ships cuDNN 8 for a TensorFlow built
against cuDNN 9 and registers no GPU. And the run report says what a species lane's scores are."""
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_hear_tag import HT, ROOT  # noqa: E402

EMBED_MANIFEST = os.path.join(ROOT, "deploy", "k8s", "hear-embed.yaml")


def _embed_script():
    with open(EMBED_MANIFEST) as fh:
        docs = [d for d in yaml.safe_load_all(fh) if d]
    c = docs[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    return c["args"][0], {e["name"]: e.get("value") for e in c["env"]}


class TestTheEmbedJobBringsItsOwnCuda:

    def test_the_pinned_cudnn9_wheels_install_without_dependencies_into_their_own_dir(self):
        script, _env = _embed_script()
        assert re.search(r"nvidia-cudnn-cu12==9\.\d", script)
        assert "--no-deps" in script and '--target "$LIB"' in script
        assert "LIB=/pool/pylib-perch" in script

    def test_every_wheel_is_pinned(self):
        script, _env = _embed_script()
        wheels = re.findall(r"(nvidia-[a-z0-9-]+)(==)?", script)
        assert wheels and all(eq for _name, eq in wheels), wheels

    def test_the_library_path_is_exported_before_the_tagger_starts(self):
        script, _env = _embed_script()
        assert script.index("export LD_LIBRARY_PATH") < script.index("exec python")
        assert script.index("export LD_LIBRARY_PATH") < script.index("--verify-weights")

    def test_a_missing_library_is_named_in_the_log(self):
        _script, env = _embed_script()
        assert int(env["TF_CPP_MIN_LOG_LEVEL"]) <= 1


class TestTheReportSaysWhatTheScoresAre:

    def _report(self, lane):
        t = HT.empty_tally()
        t.update({"lane": lane, "cap_hit": False, "stop_reason": None, "conservation_ok": True,
                  "silence_frac": None,
                  "observation_not_health": {"level_dbfs": {"n": 0}}})
        t.pop("top_scores"), t.pop("dbfs"), t.pop("unstored")
        return HT.format_report(t)

    def test_a_species_lane_says_hypotheses(self):
        r = self._report("birdnet_v24")
        assert "species hypotheses nobody has confirmed" in r and "not species IDs" not in r

    def test_other_lanes_say_not_species(self):
        for lane in ("mn10", "mn10_pad10", "perch_v2"):
            assert "not species IDs" in self._report(lane), lane
