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

    def test_a_failed_install_still_reports_its_error(self):
        script, _env = _embed_script()
        install = script[script.index("pip install"):script.index('touch "$LIB/')]
        assert ">/dev/null" in install and "2>&1" not in install


def _ld_block():
    """The manifest's own LD_LIBRARY_PATH lines, run as written."""
    script, _env = _embed_script()
    lines = script.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == 'LD=""')
    end = next(i for i, l in enumerate(lines) if l.strip().startswith("export LD_LIBRARY_PATH"))
    return "\n".join(lines[start:end + 1]) + '\nprintf "%s" "$LD_LIBRARY_PATH"\n'


def _run_ld(lib, ld_library_path=None):
    import subprocess
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LIB": str(lib)}
    if ld_library_path is not None:
        env["LD_LIBRARY_PATH"] = ld_library_path
    return subprocess.run(["/bin/sh", "-c", _ld_block()], capture_output=True, text=True,
                          env=env)


class TestTheLibraryPathHasNoEmptyElement:
    """An empty entry on LD_LIBRARY_PATH means the current directory (Copilot, #57)."""

    def _lib(self, tmp_path, names):
        for n in names:
            (tmp_path / "nvidia" / n / "lib").mkdir(parents=True)
        return tmp_path

    def test_an_empty_starting_path_adds_only_the_wheel_dirs(self, tmp_path):
        lib = self._lib(tmp_path, ["cudnn", "cublas"])
        r = _run_ld(lib)
        assert r.returncode == 0, r.stderr
        parts = r.stdout.split(":")
        assert parts == sorted(parts) and all(parts), r.stdout
        assert parts == [str(lib / "nvidia" / n / "lib") for n in ("cublas", "cudnn")]

    def test_an_existing_path_is_kept_after_the_wheels(self, tmp_path):
        lib = self._lib(tmp_path, ["cudnn"])
        r = _run_ld(lib, "/usr/local/nvidia/lib")
        assert r.stdout == "%s:/usr/local/nvidia/lib" % (lib / "nvidia" / "cudnn" / "lib")

    def test_no_wheel_dirs_fails_instead_of_adding_a_glob(self, tmp_path):
        r = _run_ld(tmp_path)
        assert r.returncode != 0 and "*" not in r.stdout


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
