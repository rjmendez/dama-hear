"""The two things the hear-drain manifest has to get right, checked by RUNNING it.

⚠️NOT BY GREPPING THE YAML. A source-scanning guard matches its own explanatory comment -- this
repo has already shipped that bug twice -- and grepping for the string "--phone-corpus" would pass
on the very comment warning that it went missing. So: the check job's script is EXTRACTED from the
manifest and EXECUTED against stub binaries, and the drain's flags are read from the parsed args.

Both defects were live on 2026-09-09 and both were silent:

  the check job could not fail   its script ended on --stats, which always succeeds, so the shell
                                 returned 0 whatever --check found. A staleness gate that cannot
                                 fail is a green light nobody reads.
  --phone-corpus existed only    it was added to the live CronJob by hand. Measured: 1 occurrence
  on the live object            in the running spec, 0 in this file, 0 in the object's own
                                 last-applied-configuration -- so the next `kubectl apply -f` of
                                 this manifest would have deleted it, and the phone leg would have
                                 gone quiet with nothing reporting the loss.
"""
import os
import re
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "deploy", "k8s", "hear-drain.yaml")

#: The path the phone corpus is written to and read from. ⚠️HALF OF A CROSS-REPO CONTRACT: the
#: other half is dama-gotchi's dama-sketch-corpus Deployment (SKETCH_CORPUS_OUT_DIR), which writes
#: this same path on this same PVC. It is written here as a literal rather than read from that
#: repo on purpose -- a test that reaches into a sibling checkout tests that checkout.
PHONE_CORPUS_PATH = "/pool/sketch_corpus"


def _containers(text):
    """{container name: its args block} straight out of the manifest text.

    Deliberately a small parser rather than a yaml dependency: the suite has no yaml requirement
    and adding one for two lookups would be the larger change.
    """
    out, name, buf, grabbing = {}, None, [], False
    for line in text.splitlines():
        m = re.match(r"\s*- name: (\S+)\s*$", line)
        if m and not grabbing:
            name = m.group(1)
        if re.match(r"\s*args:\s*$", line) and name:
            grabbing, buf = True, []
            continue
        if grabbing:
            if re.match(r"\s*(volumeMounts|resources|env|image|command):", line):
                out[name] = "\n".join(buf)
                grabbing, name = False, None
                continue
            buf.append(line)
    if grabbing and name:
        out[name] = "\n".join(buf)
    return out


@pytest.fixture(scope="module")
def blocks():
    with open(MANIFEST) as fh:
        return _containers(fh.read())


def _script(block):
    """The shell body, de-indented, with the leading `- |` stripped."""
    lines = [l for l in block.splitlines() if l.strip() and not l.strip().startswith("- |")]
    return textwrap.dedent("\n".join(lines))


class TestTheCheckJobCanActuallyFail:
    """⚠️Run it. The bug was a shell-semantics bug and only the shell can prove it gone."""

    def _run(self, script, tmp_path, check_rc):
        """Execute the check script with `python` stubbed: --check exits check_rc, --stats exits 0."""
        stub = tmp_path / "python"
        stub.write_text(
            "#!/bin/sh\n"
            "for a in \"$@\"; do\n"
            "  [ \"$a\" = \"--check\" ] && exit %d\n"
            "  [ \"$a\" = \"--stats\" ] && { echo STATS_RAN; exit 0; }\n"
            "done\nexit 0\n" % check_rc)
        stub.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (tmp_path, os.environ.get("PATH", "")))
        return subprocess.run(["/bin/sh", "-lc", script], capture_output=True, text=True, env=env)

    def test_a_failing_check_fails_the_job(self, blocks, tmp_path):
        r = self._run(_script(blocks["check"]), tmp_path, check_rc=1)
        assert r.returncode == 1, (
            "the check job returned %d for a FAILING --check. The original script ended on --stats, "
            "which always succeeds; `sh -lc 'false; echo x; true'` exits 0." % r.returncode)

    def test_a_passing_check_passes_the_job(self, blocks, tmp_path):
        assert self._run(_script(blocks["check"]), tmp_path, check_rc=0).returncode == 0

    def test_the_pool_summary_still_prints_when_the_check_failed(self, blocks, tmp_path):
        # A plain `set -e` would fix the exit code and lose this -- and a failure is exactly when
        # the summary is wanted.
        r = self._run(_script(blocks["check"]), tmp_path, check_rc=1)
        assert "STATS_RAN" in r.stdout, "--stats was skipped on failure; rc must not short-circuit it"

    def test_stats_cannot_mask_a_failing_check(self, blocks, tmp_path):
        # --stats failing too must not turn a red check green, nor a green one red.
        stub = tmp_path / "python"
        stub.write_text("#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = \"--check\" ] && exit 1; done\nexit 7\n")
        stub.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (tmp_path, os.environ.get("PATH", "")))
        r = subprocess.run(["/bin/sh", "-lc", _script(blocks["check"])],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 1, "exit code must be --check's (1), not --stats' (%d)" % r.returncode


class TestThePhoneLegIsDeclared:
    def test_the_drain_passes_phone_corpus(self, blocks):
        assert "--phone-corpus" in blocks["drain"], (
            "--phone-corpus is missing from the manifest. It was live-only once already: the next "
            "kubectl apply deletes it and the phone leg goes quiet with nothing reporting the loss.")

    def test_it_points_at_the_path_the_worker_writes(self, blocks):
        m = re.search(r"--phone-corpus\s+(\S+)", blocks["drain"])
        assert m, "--phone-corpus present but with no path"
        assert m.group(1) == PHONE_CORPUS_PATH, (
            "the drain reads %r but dama-gotchi's dama-sketch-corpus writes %r -- every phone row "
            "would be stranded, and both halves look correct in isolation."
            % (m.group(1), PHONE_CORPUS_PATH))

    def test_the_pool_volume_is_mounted_so_that_path_exists(self):
        with open(MANIFEST) as fh:
            text = fh.read()
        assert text.count("mountPath: /pool") >= 2, (
            "both the drain and the check must mount the pool PVC; %r lives on it"
            % PHONE_CORPUS_PATH)
