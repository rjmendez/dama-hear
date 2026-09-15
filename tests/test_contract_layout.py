"""tools/check_contract_layout.py: a published contract cannot lose its home.

Each test builds a synthetic tree rather than asserting against the live repository, so the
rules stay provable while `contracts/` is still arriving on another branch.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check_contract_layout.py"

sys.path.insert(0, str(ROOT))

from tools import check_contract_layout as CL  # noqa: E402

CID = "hear.example.v1"


def _tree(base: Path, *, contract: bool = True) -> Path:
    """A minimal compliant checkout: one contract with every required companion."""
    (base / "requirements").mkdir(parents=True)
    (base / "requirements" / "ci-dev.txt").write_text("numpy==2.4.4\nPyYAML==6.0.2\n")

    (base / "docs" / "data").mkdir(parents=True)
    (base / "docs" / "decisions").mkdir(parents=True)
    (base / "docs" / "decisions" / "0002-example.md").write_text(
        f"# 0002\n\n- Status: accepted\n\nDefines `{CID}`.\n"
    )
    (base / "docs" / "data" / "phase0-freeze-contracts.v1.json").write_text(
        json.dumps({"schemas": {"schema_identifiers": [
            {"schema": CID, "source_paths": ["hear/ingest/envelope.py"]},
        ]}})
    )

    (base / ".github" / "workflows").mkdir(parents=True)
    (base / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  gen:\n    steps:\n      - run: python tools/gen_example.py\n"
    )
    (base / "tools").mkdir(exist_ok=True)
    (base / "tools" / "gen_example.py").write_text(f'"""generates {CID}."""\n')

    (base / "hear" / "ingest").mkdir(parents=True)
    (base / "hear" / "ingest" / "envelope.py").write_text("import json\nimport hashlib\n")

    if contract:
        (base / "contracts" / "schemas").mkdir(parents=True)
        (base / "contracts" / "schemas" / f"{CID}.schema.json").write_text("{}\n")
        fixtures = base / "contracts" / "fixtures" / CID
        fixtures.mkdir(parents=True)
        (fixtures / "valid.json").write_text("{}\n")
        (fixtures / "manifest.json").write_text(
            json.dumps({"fixtures": [{"file": "valid.json", "expect": "accepted"}]})
        )
    return base


def test_a_compliant_tree_passes(tmp_path):
    assert CL.run(_tree(tmp_path)) == []


def test_a_checkout_without_contracts_passes_vacuously(tmp_path):
    """The gate arms when the directory appears, so it can land before the first contract."""
    assert CL.run(_tree(tmp_path, contract=False)) == []


def test_the_live_repository_passes():
    assert CL.run(ROOT) == []


def test_a_contract_with_no_recorded_source_of_truth_fails(tmp_path):
    base = _tree(tmp_path)
    (base / "docs" / "data" / "phase0-freeze-contracts.v1.json").write_text(
        json.dumps({"schemas": {"schema_identifiers": []}})
    )
    assert any("source of truth" in p for p in CL.run(base))


def test_a_contract_with_no_decision_record_fails(tmp_path):
    base = _tree(tmp_path)
    (base / "docs" / "decisions" / "0002-example.md").unlink()
    assert any("decision record" in p for p in CL.run(base))


def test_a_hand_maintained_artifact_fails(tmp_path):
    base = _tree(tmp_path)
    (base / "tools" / "gen_example.py").unlink()
    assert any("no tools/gen_" in p for p in CL.run(base))


def test_a_generator_missing_from_ci_fails(tmp_path):
    """An artifact whose generator has no CI drift gate stops describing its source."""
    base = _tree(tmp_path)
    (base / ".github" / "workflows" / "ci.yml").write_text("jobs:\n  tests:\n    steps: []\n")
    assert any("not wired into" in p for p in CL.run(base))


def test_an_undeclared_fixture_fails(tmp_path):
    base = _tree(tmp_path)
    (base / "contracts" / "fixtures" / CID / "stray.json").write_text("{}\n")
    assert any("stray.json" in p for p in CL.run(base))


def test_fixtures_without_a_manifest_fail(tmp_path):
    base = _tree(tmp_path)
    (base / "contracts" / "fixtures" / CID / "manifest.json").unlink()
    assert any("manifest.json is missing" in p for p in CL.run(base))


def test_code_inside_the_published_home_fails(tmp_path):
    """contracts/ is the published form of a contract, not a second place to put logic."""
    base = _tree(tmp_path)
    (base / "contracts" / "schemas" / "helper.py").write_text("x = 1\n")
    assert any("generated artifacts only" in p for p in CL.run(base))


def test_a_pinned_dependency_in_the_contract_core_fails(tmp_path):
    base = _tree(tmp_path)
    (base / "hear" / "ingest" / "envelope.py").write_text("import numpy\n")
    assert any("imports 'numpy'" in p for p in CL.run(base))


def test_an_unpinned_transport_import_in_the_contract_core_fails(tmp_path):
    """boto3 is not in any requirements file; it is forbidden in the core regardless."""
    base = _tree(tmp_path)
    (base / "hear" / "ingest" / "envelope.py").write_text("from boto3 import client\n")
    assert any("imports 'boto3'" in p for p in CL.run(base))


def test_a_stdlib_only_contract_core_passes(tmp_path):
    base = _tree(tmp_path)
    (base / "hear" / "ingest" / "envelope.py").write_text(
        "import hashlib\nimport json\nfrom typing import Any\n"
    )
    assert CL.run(base) == []


def test_duplicate_decision_numbers_fail(tmp_path):
    base = _tree(tmp_path)
    (base / "docs" / "decisions" / "0002-other.md").write_text("- Status: draft\n")
    assert any("already used by" in p for p in CL.run(base))


def test_a_decision_record_without_a_status_fails(tmp_path):
    base = _tree(tmp_path)
    (base / "docs" / "decisions" / "0002-example.md").write_text(f"# 0002\n\n{CID}\n")
    assert any("no 'Status:' line" in p for p in CL.run(base))


def test_a_misnamed_decision_record_fails(tmp_path):
    base = _tree(tmp_path)
    (base / "docs" / "decisions" / "example.md").write_text("- Status: accepted\n")
    assert any("NNNN-kebab-title" in p for p in CL.run(base))


def test_requirement_names_map_to_import_names(tmp_path):
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements" / "ci.txt").write_text(
        "PyYAML==6.0.2\nscikit-learn==1.8.0\npaho-mqtt==1.6.1\n# comment\n\n"
    )
    assert CL.requirement_distributions(tmp_path) == {"yaml", "sklearn", "paho"}


def test_the_cli_reports_violations_and_exits_nonzero(tmp_path):
    base = _tree(tmp_path)
    (base / "contracts" / "schemas" / "helper.py").write_text("x = 1\n")
    got = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(base)], capture_output=True, text=True
    )
    assert got.returncode == 1
    assert "generated artifacts only" in got.stderr
    assert "0002-contract-repository-layout.md" in got.stderr


def test_the_cli_passes_on_the_live_repository():
    got = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True)
    assert got.returncode == 0, got.stderr
