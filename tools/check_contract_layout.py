#!/usr/bin/env python3
"""Enforce where a published contract may live and what must ship with it.

    python3 tools/check_contract_layout.py

`docs/decisions/0002-contract-repository-layout.md` states the rules; this tool is the
merge-blocking form of them. It reads only the checked-in tree: no cluster, no network, no
third-party import, so it runs on a bare interpreter before any requirements file is
installed.

The rules are deliberately conditional on what exists. A checkout with no `contracts/`
directory passes vacuously, which is what lets this land independently of the lane that
creates the first contract: the gate is armed the moment the directory appears, and never
before.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

ROOT = Path(__file__).resolve().parents[1]

#: The published home. Generated artifacts only; see rule 7.
CONTRACTS_DIRNAME = "contracts"
#: Deliberately not named `*SCHEMA*`: `tools/freeze_contracts.py` harvests contract ids from
#: `[A-Z_]*SCHEMA[A-Z_]* = "..."` assignments, so that name would register ".schema.json" as
#: a contract in the frozen baseline.
PUBLISHED_SUFFIX = ".schema.json"

#: Paths that must stay installable-free so a contract can be validated from a bare
#: interpreter and from a clean-room adapter test. Checked when present.
DEPENDENCY_FREE_PATHS = ("hear/ingest", "contracts", "tools/check_contract_layout.py")

#: Imports that are never allowed inside DEPENDENCY_FREE_PATHS regardless of whether a
#: requirements file happens to pin them today. Transport/cloud/vendor coupling in the
#: contract core is the recoupling failure this exists to catch.
ALWAYS_FORBIDDEN = frozenset({
    "boto3", "botocore", "awscli", "redis", "paho", "kubernetes", "android",
})

DECISION_DIR = "docs/decisions"
DECISION_NAME = re.compile(r"^(\d{4})-[a-z0-9][a-z0-9-]*\.md$")
CI_WORKFLOW = ".github/workflows/ci.yml"
BASELINE_JSON = "docs/data/phase0-freeze-contracts.v1.json"


class Failure(Exception):
    pass


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def requirement_distributions(root: Path) -> Set[str]:
    """Top-level import names pinned by any requirements file.

    A pin is a declaration that the package is not in the standard library, which is the
    only property this check needs. The name mapping covers the pins this repository
    actually uses; an unmapped pin falls back to its lowercased project name.
    """
    mapped = {
        "pyyaml": "yaml",
        "scikit-learn": "sklearn",
        "paho-mqtt": "paho",
        "python-dateutil": "dateutil",
        "pillow": "PIL",
    }
    names: Set[str] = set()
    req = root / "requirements"
    if not req.is_dir():
        return names
    for path in sorted(req.glob("*.txt")):
        for line in path.read_text(errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            project = re.split(r"[=<>!~\[; ]", line, 1)[0].strip().lower()
            if project:
                names.add(mapped.get(project, project.replace("-", "_")))
    return names


def python_files(root: Path, spec: str) -> List[Path]:
    target = root / spec
    if target.is_file():
        return [target] if target.suffix == ".py" else []
    if target.is_dir():
        return sorted(p for p in target.rglob("*.py") if p.is_file())
    return []


def imported_roots(path: Path) -> Set[str]:
    try:
        tree = ast.parse(path.read_text(errors="replace"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - a syntax error is its own failure
        raise Failure(f"{path}: cannot parse ({exc})") from exc
    roots: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def baseline_schema_sources(root: Path) -> Dict[str, List[str]]:
    """contract id -> source paths, as recorded by the Phase 0 freeze baseline."""
    path = root / BASELINE_JSON
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    entries = data.get("schemas", {}).get("schema_identifiers", [])
    return {item["schema"]: list(item.get("source_paths", [])) for item in entries}


def decision_records(root: Path) -> List[Path]:
    base = root / DECISION_DIR
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("*.md") if p.is_file())


def contract_ids(root: Path) -> List[str]:
    """Every contract id with a published artifact, from schemas and fixtures alike."""
    base = root / CONTRACTS_DIRNAME
    ids: Set[str] = set()
    schemas = base / "schemas"
    if schemas.is_dir():
        for path in sorted(schemas.glob("*" + PUBLISHED_SUFFIX)):
            ids.add(path.name[: -len(PUBLISHED_SUFFIX)])
    fixtures = base / "fixtures"
    if fixtures.is_dir():
        for path in sorted(fixtures.iterdir()):
            if path.is_dir():
                ids.add(path.name)
    return sorted(ids)


def _generators_for(root: Path, contract_id: str) -> List[Path]:
    tools = root / "tools"
    if not tools.is_dir():
        return []
    hits = []
    for path in sorted(tools.glob("gen_*.py")):
        if contract_id in path.read_text(errors="replace"):
            hits.append(path)
    return hits


def check_generated_only(root: Path, problems: List[str]) -> None:
    """Rule 7: the published home holds published artifacts, never source or logic."""
    base = root / CONTRACTS_DIRNAME
    if not base.is_dir():
        return
    allowed = {".json", ".md", ".yaml", ".yml", ".proto"}
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.suffix not in allowed:
            problems.append(
                f"{_rel(root, path)}: {CONTRACTS_DIRNAME}/ holds generated artifacts only; "
                f"hand-written logic belongs with its owning module"
            )


def check_contract_has_home(root: Path, problems: List[str]) -> None:
    """Rules 1-4: a published contract carries source, decision, generator and manifest."""
    ids = contract_ids(root)
    if not ids:
        return
    sources = baseline_schema_sources(root)
    decisions = {p: p.read_text(errors="replace") for p in decision_records(root)}
    ci_text = ""
    ci_path = root / CI_WORKFLOW
    if ci_path.is_file():
        ci_text = ci_path.read_text(errors="replace")

    for cid in ids:
        if cid not in sources or not sources[cid]:
            problems.append(
                f"{cid}: no source of truth recorded in {BASELINE_JSON}; regenerate the "
                f"freeze baseline so the contract's owning module is inventoried"
            )
        if not any(cid in text for text in decisions.values()):
            problems.append(
                f"{cid}: no decision record in {DECISION_DIR}/ names it; a published "
                f"contract states why it exists and what its compatibility rules are"
            )

        generators = _generators_for(root, cid)
        if not generators:
            problems.append(
                f"{cid}: no tools/gen_*.py produces it; contract artifacts are generated, "
                f"not hand-maintained"
            )
        elif ci_text and not any(_rel(root, g) in ci_text for g in generators):
            listed = ", ".join(_rel(root, g) for g in generators)
            problems.append(
                f"{cid}: generator not wired into {CI_WORKFLOW} ({listed}); an artifact "
                f"with no drift gate silently stops describing its source"
            )

        fixtures = root / CONTRACTS_DIRNAME / "fixtures" / cid
        if fixtures.is_dir():
            manifest = fixtures / "manifest.json"
            if not manifest.is_file():
                problems.append(
                    f"{cid}: contracts/fixtures/{cid}/manifest.json is missing; a fixture "
                    f"without a declared expected outcome cannot be asserted against"
                )
                continue
            declared = _declared_fixture_names(manifest)
            for path in sorted(fixtures.glob("*.json")):
                if path.name == "manifest.json":
                    continue
                if path.name not in declared:
                    problems.append(
                        f"{cid}: {_rel(root, path)} is not listed in manifest.json with an "
                        f"expected outcome"
                    )


def _declared_fixture_names(manifest: Path) -> Set[str]:
    try:
        data = json.loads(manifest.read_text())
    except json.JSONDecodeError as exc:
        raise Failure(f"{manifest}: invalid JSON ({exc})") from exc
    names: Set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and value.endswith(".json"):
                    names.add(Path(value).name)
                if isinstance(key, str) and key.endswith(".json"):
                    names.add(Path(key).name)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and node.endswith(".json"):
            names.add(Path(node).name)

    walk(data)
    return names


def check_dependency_free(root: Path, problems: List[str]) -> None:
    """Rule 5: the contract core validates on a bare interpreter."""
    forbidden = requirement_distributions(root) | ALWAYS_FORBIDDEN
    for spec in DEPENDENCY_FREE_PATHS:
        for path in python_files(root, spec):
            for name in sorted(imported_roots(path) & forbidden):
                problems.append(
                    f"{_rel(root, path)}: imports '{name}'; {spec} must stay importable "
                    f"with no installed dependency so a clean-room adapter can validate"
                )


def check_decision_records(root: Path, problems: List[str]) -> None:
    """Rule 6: decision records are uniquely numbered and state a status."""
    seen: Dict[str, str] = {}
    for path in decision_records(root):
        match = DECISION_NAME.match(path.name)
        if not match:
            problems.append(
                f"{_rel(root, path)}: decision records are named NNNN-kebab-title.md"
            )
            continue
        number = match.group(1)
        if number in seen:
            problems.append(
                f"{_rel(root, path)}: decision number {number} already used by {seen[number]}"
            )
        seen[number] = path.name
        if not re.search(r"^[-*]?\s*Status:", path.read_text(errors="replace"), re.M):
            problems.append(f"{_rel(root, path)}: no 'Status:' line")


def run(root: Path) -> List[str]:
    problems: List[str] = []
    check_generated_only(root, problems)
    check_contract_has_home(root, problems)
    check_dependency_free(root, problems)
    check_decision_records(root, problems)
    return problems


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(ROOT), help="repository root to check")
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).resolve()
    try:
        problems = run(root)
    except Failure as exc:
        print(f"contract layout: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("contract layout violations:", file=sys.stderr)
        for item in problems:
            print(f"  {item}", file=sys.stderr)
        print(
            "see docs/decisions/0002-contract-repository-layout.md for the rule and the fix",
            file=sys.stderr,
        )
        return 1
    print(f"contract layout ok ({len(contract_ids(root))} published contract(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
