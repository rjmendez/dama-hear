"""The CI job that stands in for the pods has to install what the pods install."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIN = re.compile(r"\b(numpy|scipy)==([0-9][0-9.]*)")


def test_the_pods_job_pins_what_the_manifests_install():
    ci = dict(PIN.findall((ROOT / "requirements" / "ci-pods.txt").read_text()))
    installed = {}
    for m in sorted((ROOT / "deploy" / "k8s").glob("hear-*.yaml")):
        if m.name.endswith("-code.yaml"):
            continue
        for name, ver in PIN.findall(m.read_text()):
            installed.setdefault(name, {}).setdefault(ver, []).append(m.name)
    assert installed, "no pinned numpy or scipy found in deploy/k8s/hear-*.yaml"
    for name, by_ver in installed.items():
        assert len(by_ver) == 1, "the manifests disagree on %s: %s" % (name, by_ver)
        (want,) = by_ver
        assert ci.get(name) == want, (
            "requirements/ci-pods.txt pins %s %s, the pods install %s (%s)"
            % (name, ci.get(name), want, ", ".join(by_ver[want])))
