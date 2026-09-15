#!/usr/bin/env python3
"""Fail when the standalone core re-couples itself to a transport, a cloud or a shared volume.

    python3 tools/recoupling_guard.py                 # scan hear/ and modules/
    python3 tools/recoupling_guard.py --roots hear    # scan one root
    python3 tools/recoupling_guard.py --list-rules    # what is refused and why

`docs/standalone-migration.md` names these as MERGE-BLOCKING, not as advice: "imports from
gotchi/cloud/deployment packages into `hear/` or `modules/`; core code naming AWS, Oxalis, Redis
keys, PVC paths, Android classes or `gotchi-phone`; direct core writes to Redis/MQTT/PVC". The
core may be depended ON by an adapter and may never depend on one, so the boundary has to be
checked by something that runs on every change rather than remembered.

⚠️AST, NOT grep. `hear/nodeclass.py` cites dama-gotchi's own calibration files in twenty comments
and `hear/corpus.py` quotes an Android source line, because that is where those numbers came
from; a text scan calls every one of them a dependency and the gate gets turned off within a
week. This module parses each file and looks at exactly two things -- what it IMPORTS, and the
string literals it EVALUATES -- so a citation in a comment or a docstring is free and a coupling
is not. A docstring is skipped for the same reason a comment is: it is documentation the
interpreter never dials.

⚠️A KNOWN COUPLING IS ALLOWLISTED BY VALUE, NEVER BY FILE. `tools/recoupling_allow.txt` holds one
line per (path, rule, exact token) with a reason, in the shape `tools/coord_guard_allow.txt`
already established here. Allowing a whole file would let the NEXT PVC path into the same module
unnoticed, which is the failure this gate exists to stop. An allow line that no longer matches
anything is itself an error, so the list cannot rot into a blanket exemption.

⚠️THIS GATE DOES NOT MOVE EXISTING BEHAVIOUR. It adds no runtime code path and changes no
default. The one shipped coupling it finds -- `hear/spatial.py`'s `/pool` default -- is recorded
with its reason rather than edited, because changing where a shipped service writes is a rollout
with a rollback plan and not a lint fix. What the gate buys today is that the SECOND one cannot
arrive silently.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOTS: Tuple[str, ...] = ("hear", "modules")
ALLOW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recoupling_allow.txt")
ALLOW_SEP = " :: "


@dataclass(frozen=True)
class Rule:
    """One refusal: what it catches, and the sentence a reviewer needs to act on it."""
    name: str
    why: str
    #: Top-level distribution names whose import is the coupling itself.
    imports: frozenset = frozenset()
    #: Evaluated string literals that carry the coupling even with no import.
    literal: Optional[re.Pattern] = None


RULES: Tuple[Rule, ...] = (
    Rule(
        "redis",
        "Redis is a cache and a lease, never the core's truth: a core that imports a client or "
        "spells a `dama:hear:*` key has made cache loss into data loss.",
        imports=frozenset({"redis", "aioredis", "walrus", "redis_om"}),
        literal=re.compile(r"redis://|rediss://|\bdama:hear:"),
    ),
    Rule(
        "aws",
        "AWS/Oxalis is one optional egress adapter. An ARN, an endpoint host or an SDK import "
        "inside the core makes a cloud account a prerequisite for a node that runs on a bench.",
        imports=frozenset({"boto3", "botocore", "aiobotocore", "s3transfer", "awscli", "awscrt"}),
        literal=re.compile(r"amazonaws\.com|\barn:aws:|AWS_(?:ACCESS_KEY|SECRET_ACCESS_KEY|"
                           r"SESSION_TOKEN)|\boxalis\b", re.I),
    ),
    Rule(
        "mqtt",
        "MQTT is live telemetry, not persistence proof. The core hands a record to a sink; it "
        "does not open a broker connection or own a topic namespace.",
        imports=frozenset({"paho", "gmqtt", "asyncio_mqtt", "aiomqtt", "amqtt"}),
    ),
    Rule(
        "pvc_path",
        "A shared application volume is how two workloads started reading each other's "
        "directories. Storage location is a deployment decision handed in, not a constant.",
        literal=re.compile(r"(?:^|[\"'\s=(,])/(?:pool|mnt/pool)(?:/|\b)"),
    ),
    Rule(
        "android_gotchi",
        "dama-gotchi is an optional producer behind an adapter. Its package names, Android class "
        "names and `gotchi-phone` identifiers must not be things the core dials or branches on.",
        imports=frozenset({"gotchi", "dama_gotchi", "android", "jnius", "pyjnius"}),
        literal=re.compile(r"com\.android\.|Landroid/|android\.(?:media|hardware)\.|"
                           r"\bgotchi-phone\b"),
    ),
    Rule(
        "deployment_client",
        "Scheduling and packaging are outside the core. A Kubernetes or Docker client in `hear/` "
        "means the domain code cannot be unit-tested without a cluster.",
        imports=frozenset({"kubernetes", "docker", "openshift", "kubernetes_asyncio"}),
    ),
)

RULES_BY_NAME: Dict[str, Rule] = {r.name: r for r in RULES}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    token: str

    def allow_key(self) -> Tuple[str, str, str]:
        return (self.path, self.rule, self.token)

    def render(self) -> str:
        return "%s:%d: %s: %s" % (self.path, self.line, self.rule, self.token)


def _docstring_nodes(tree: ast.AST) -> Set[int]:
    """`id()` of every string constant that is a docstring, module/class/function alike.

    A docstring is prose the interpreter stores and never dials, so it is evidence about the
    code and not a use of the thing it names -- the same standing a comment has, and comments
    are not in the AST at all.
    """
    out: Set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            out.add(id(first.value))
    return out


def _import_roots(node: ast.AST) -> List[Tuple[str, str]]:
    """(root distribution name, the text a reader should see) for one import statement."""
    out: List[Tuple[str, str]] = []
    if isinstance(node, ast.Import):
        for a in node.names:
            out.append((a.name.split(".")[0], "import %s" % a.name))
    elif isinstance(node, ast.ImportFrom):
        # A relative import names this package, never another distribution.
        if node.level:
            return out
        mod = node.module or ""
        if mod:
            out.append((mod.split(".")[0], "from %s import %s"
                        % (mod, ", ".join(a.name for a in node.names))))
    return out


def scan_source(path: str, text: str) -> List[Violation]:
    """Every coupling one file evaluates. Raises SyntaxError only on a file Python cannot parse."""
    tree = ast.parse(text, filename=path)
    docstrings = _docstring_nodes(tree)
    found: List[Violation] = []
    for node in ast.walk(tree):
        for root, shown in _import_roots(node):
            for rule in RULES:
                if root in rule.imports:
                    found.append(Violation(path, node.lineno, rule.name, shown))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            for rule in RULES:
                if rule.literal is None:
                    continue
                m = rule.literal.search(node.value)
                if m:
                    found.append(Violation(path, node.lineno, rule.name, node.value))
    found.sort(key=lambda v: (v.path, v.line, v.rule, v.token))
    return found


def python_files(roots: Sequence[str], repo: Optional[str] = None) -> List[str]:
    """Every `.py` under the scanned roots, repo-relative and sorted, for a stable report.

    `repo` resolves at CALL time rather than in the signature default, so a test may point the
    whole gate at a synthetic tree -- a gate nobody can exercise against a coupled tree is one
    nobody can show is armed.
    """
    repo = REPO if repo is None else repo
    out: List[str] = []
    for root in roots:
        base = os.path.join(repo, root)
        if os.path.isfile(base) and base.endswith(".py"):
            out.append(os.path.relpath(base, repo).replace(os.sep, "/"))
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in {"__pycache__", ".git"} and not d.startswith("."))
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, fn), repo)
                    out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def scan(roots: Sequence[str] = DEFAULT_ROOTS, repo: Optional[str] = None) -> List[Violation]:
    repo = REPO if repo is None else repo
    found: List[Violation] = []
    for rel in python_files(roots, repo):
        with open(os.path.join(repo, rel), "r", encoding="utf-8", errors="replace") as fh:
            found.extend(scan_source(rel, fh.read()))
    return found


def parse_allow(text: str) -> List[Tuple[str, str, str, str]]:
    """(path, rule, token, reason) per allow line. A malformed line raises; it is not skipped."""
    out: List[Tuple[str, str, str, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split(ALLOW_SEP)]
        if len(parts) != 4:
            raise ValueError("allow line %d is not `path%srule%stoken%sreason`: %r"
                             % (n, ALLOW_SEP, ALLOW_SEP, ALLOW_SEP, line))
        path, rule, token, reason = parts
        if rule not in RULES_BY_NAME:
            raise ValueError("allow line %d names unknown rule %r (known: %s)"
                             % (n, rule, ", ".join(sorted(RULES_BY_NAME))))
        if len(reason) < 20:
            raise ValueError("allow line %d states no reason: %r" % (n, reason))
        out.append((path, rule, token, reason))
    return out


def load_allow(path: Optional[str] = None) -> List[Tuple[str, str, str, str]]:
    path = ALLOW_FILE if path is None else path
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return parse_allow(fh.read())


def check(roots: Sequence[str] = DEFAULT_ROOTS, repo: Optional[str] = None,
          allow: Optional[Iterable[Tuple[str, str, str, str]]] = None
          ) -> Tuple[List[Violation], List[Tuple[str, str, str, str]]]:
    """(couplings nothing has declared, allow entries that matched nothing).

    Both halves fail the gate. An unmatched allow entry is not harmless: it is an exemption
    nobody can see the subject of any more, and leaving it standing is how a value-scoped list
    turns into a file-scoped one.
    """
    entries = list(load_allow() if allow is None else allow)
    allowed = {(p, r, t) for p, r, t, _ in entries}
    hit: Set[Tuple[str, str, str]] = set()
    remaining: List[Violation] = []
    for v in scan(roots, repo):
        k = v.allow_key()
        if k in allowed:
            hit.add(k)
            continue
        remaining.append(v)
    stale = [e for e in entries if (e[0], e[1], e[2]) not in hit]
    return remaining, stale


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roots", nargs="+", default=list(DEFAULT_ROOTS),
                    help="repo-relative directories or files to scan")
    ap.add_argument("--list-rules", action="store_true", help="print the rules and exit")
    a = ap.parse_args(argv)

    if a.list_rules:
        for rule in RULES:
            sys.stdout.write("%s\n  %s\n" % (rule.name, rule.why))
            if rule.imports:
                sys.stdout.write("  imports: %s\n" % ", ".join(sorted(rule.imports)))
            if rule.literal is not None:
                sys.stdout.write("  literals: %s\n" % rule.literal.pattern)
        return 0

    try:
        bad, stale = check(a.roots)
    except (SyntaxError, ValueError) as e:
        sys.stderr.write("recoupling_guard: %s\n" % e)
        return 2

    if not bad and not stale:
        sys.stdout.write("recoupling_guard: %s clean (%d file(s))\n"
                         % (", ".join(a.roots), len(python_files(a.roots))))
        return 0
    for v in bad:
        sys.stderr.write("%s\n    %s\n" % (v.render(), RULES_BY_NAME[v.rule].why))
    for path, rule, token, _ in stale:
        sys.stderr.write("%s: allow entry for %s matches nothing any more: %s\n"
                         % (path, rule, token))
    if bad:
        sys.stderr.write(
            "\nThe core may be depended ON by an adapter and may never depend on one "
            "(docs/standalone-migration.md). Move the coupling behind a port, or -- if it is a "
            "shipped default that needs its own rollout -- record it in %s with a reason.\n"
            % os.path.relpath(ALLOW_FILE, REPO).replace(os.sep, "/"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
