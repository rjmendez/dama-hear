#!/usr/bin/env python3
"""The recoupling gate's own tests: what it catches, what it must NOT catch, and that it is green.

⚠️THE SECOND HALF IS THE LOAD-BEARING ONE. A boundary gate that fires on a comment gets switched
off in a week, and a repository whose core cites dama-gotchi's calibration file in twenty places
and quotes an Android source line in another is exactly the repository where that happens. Every
"does not fire" case below is a real construct from `hear/`, so a future tightening of the rules
has to keep the citations free or it fails here first.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import recoupling_guard as RG                            # noqa: E402

REPO = RG.REPO


def _scan(src: str, path: str = "hear/fake.py"):
    return RG.scan_source(path, src)


def _rules(vs):
    return sorted(v.rule for v in vs)


class TestTheRepositoryIsClean:
    """The merge-blocking assertion. Everything else in this file is about trusting this one."""

    def test_the_core_carries_no_undeclared_coupling(self):
        bad, stale = RG.check()
        assert bad == [], "\n".join(v.render() for v in bad)

    def test_no_allow_entry_has_outlived_the_thing_it_allowed(self):
        """An exemption nobody can see the subject of any more is how a value-scoped allowlist
        turns into a file-scoped one."""
        _, stale = RG.check()
        assert stale == [], stale

    def test_every_allow_entry_states_a_reason_and_a_known_rule(self):
        entries = RG.load_allow()
        assert entries, "the shipped couplings are recorded; an empty list means the file moved"
        for path, rule, token, reason in entries:
            assert rule in RG.RULES_BY_NAME
            assert os.path.exists(os.path.join(REPO, path)), path
            assert len(reason) > 40, (path, reason)

    def test_the_scan_covers_the_packages_the_migration_calls_the_core(self):
        files = RG.python_files(RG.DEFAULT_ROOTS)
        assert "hear/pool.py" in files
        assert "hear/backend/associate.py" in files
        assert any(f.startswith("modules/") for f in files)
        assert not any(f.startswith(("tools/", "tests/")) for f in files)


class TestItCatchesTheCouplings:
    """One case per rule, in the shape the coupling actually arrives in."""

    def test_a_redis_client_import(self):
        assert _rules(_scan("import redis\n")) == ["redis"]
        assert _rules(_scan("from redis.asyncio import Redis\n")) == ["redis"]

    def test_a_redis_key_namespace_spelled_in_the_core(self):
        assert _rules(_scan('KEY = "dama:hear:heartbeat:%s"\n')) == ["redis"]
        assert _rules(_scan('URL = "redis://audit-redis.infra.svc.cluster.local:6379"\n')) \
            == ["redis"]

    def test_an_aws_sdk_import_or_endpoint(self):
        assert _rules(_scan("import boto3\n")) == ["aws"]
        assert _rules(_scan('B = "s3.us-east-1.amazonaws.com"\n')) == ["aws"]
        assert _rules(_scan('R = "arn:aws:iot:us-east-1:1:thing/gold"\n')) == ["aws"]

    def test_an_oxalis_reference(self):
        assert _rules(_scan('HOST = "oxalis-ingest"\n')) == ["aws"]

    def test_an_mqtt_client_import_even_inside_a_function(self):
        src = "def publish():\n    import paho.mqtt.client as mqtt\n    return mqtt\n"
        assert _rules(_scan(src)) == ["mqtt"]

    def test_a_shared_volume_path(self):
        assert _rules(_scan('ROOT = "/pool/corpus"\n')) == ["pvc_path"]
        assert _rules(_scan('ROOT = "/mnt/pool/corpus"\n')) == ["pvc_path"]

    def test_an_android_class_or_gotchi_package(self):
        assert _rules(_scan('C = "android.media.AudioTimestamp"\n')) == ["android_gotchi"]
        assert _rules(_scan("import dama_gotchi\n")) == ["android_gotchi"]

    def test_a_deployment_client(self):
        assert _rules(_scan("from kubernetes import client\n")) == ["deployment_client"]

    def test_the_violation_names_the_line_it_is_on(self):
        v = _scan("x = 1\n\nimport boto3\n")[0]
        assert v.line == 3
        assert "boto3" in v.render() and "hear/fake.py:3" in v.render()


class TestItDoesNotCatchEvidence:
    """Citations, provenance and prose are why the core is trustworthy, not why it is coupled."""

    def test_a_comment_citing_a_gotchi_source_file_is_free(self):
        src = ("# dama-gotchi/android/app/src/main/assets/acoustic_latency_calibration.json,\n"
               "# read 2026-09-10; sensors/tdoa_triangulation.py TIER_DEFAULT_SIGMA_NS.\n"
               "SIGMA_NS = 5_000_000\n")
        assert _scan(src) == []

    def test_a_docstring_quoting_an_android_constant_is_free(self):
        src = ('"""EskfFusion.kt:318 BAD_PEER_CLOCK_TIERS, and android.media.AudioTimestamp."""\n'
               "TIERS = frozenset({'gnss', 'location'})\n")
        assert _scan(src) == []

    def test_a_function_docstring_naming_a_pool_path_is_free(self):
        src = ("def read():\n"
               '    """Historically this came off /pool/corpus; the caller now names the root."""\n'
               "    return None\n")
        assert _scan(src) == []

    def test_a_relative_import_is_never_a_foreign_distribution(self):
        assert _scan("from . import redis_like\nfrom .redis import x\n") == []

    def test_an_unrelated_absolute_path_is_not_a_shared_volume(self):
        assert _scan('P = "/var/lib/hear/state.json"\n') == []
        assert _scan('P = "/poolside/notes"\n') == []

    def test_the_word_redis_inside_an_ordinary_word_is_not_a_client(self):
        assert _scan('MSG = "rediscover the origin"\n') == []


class TestTheAllowlistIsValueScoped:
    """Allowing a file would let the NEXT coupling into that file through unnoticed."""

    def test_an_entry_allows_only_its_own_token(self, tmp_path, monkeypatch):
        src = 'A = "/pool/one"\nB = "/pool/two"\n'
        mod = tmp_path / "hear"
        mod.mkdir()
        (mod / "m.py").write_text(src)
        allow = [("hear/m.py", "pvc_path", "/pool/one", "declared for this one value only")]
        bad, stale = RG.check(roots=["hear"], repo=str(tmp_path), allow=allow)
        assert [v.token for v in bad] == ["/pool/two"]
        assert stale == []

    def test_an_entry_that_matches_nothing_is_itself_a_failure(self, tmp_path):
        mod = tmp_path / "hear"
        mod.mkdir()
        (mod / "m.py").write_text("A = 1\n")
        allow = [("hear/m.py", "pvc_path", "/pool/gone", "removed in a later commit, allow left")]
        bad, stale = RG.check(roots=["hear"], repo=str(tmp_path), allow=allow)
        assert bad == []
        assert [e[2] for e in stale] == ["/pool/gone"]

    def test_an_entry_does_not_leak_across_files(self, tmp_path):
        for name in ("a.py", "b.py"):
            d = tmp_path / "hear"
            d.mkdir(exist_ok=True)
            (d / name).write_text('P = "/pool/shared"\n')
        allow = [("hear/a.py", "pvc_path", "/pool/shared", "one file's declared legacy default")]
        bad, _ = RG.check(roots=["hear"], repo=str(tmp_path), allow=allow)
        assert [v.path for v in bad] == ["hear/b.py"]


class TestTheAllowFileParser:
    """A malformed exemption must fail loudly; a silently skipped line is an exemption too."""

    def test_a_well_formed_line_round_trips(self):
        line = "hear/x.py :: pvc_path :: /pool/y :: because it shipped and moves in phase 3"
        assert RG.parse_allow(line) == [
            ("hear/x.py", "pvc_path", "/pool/y", "because it shipped and moves in phase 3")]

    def test_comments_and_blank_lines_are_ignored(self):
        assert RG.parse_allow("# a header\n\n   \n") == []

    def test_a_missing_field_raises_rather_than_being_skipped(self):
        with pytest.raises(ValueError):
            RG.parse_allow("hear/x.py :: pvc_path :: /pool/y\n")

    def test_an_unknown_rule_raises(self):
        with pytest.raises(ValueError):
            RG.parse_allow("hear/x.py :: invented :: /pool/y :: a reason long enough to pass\n")

    def test_an_unreasoned_entry_raises(self):
        with pytest.raises(ValueError):
            RG.parse_allow("hear/x.py :: pvc_path :: /pool/y :: because\n")


class TestTheCommandLine:
    """CI runs the module, not the function, so the exit codes are part of the contract."""

    def test_a_clean_tree_exits_zero(self, capsys):
        assert RG.main([]) == 0
        assert "clean" in capsys.readouterr().out

    def test_a_coupled_tree_exits_one_and_says_where(self, tmp_path, monkeypatch, capsys):
        pkg = tmp_path / "hear"
        pkg.mkdir()
        (pkg / "m.py").write_text("import boto3\n")
        monkeypatch.setattr(RG, "REPO", str(tmp_path))
        monkeypatch.setattr(RG, "ALLOW_FILE", str(tmp_path / "absent.txt"))
        monkeypatch.setattr(RG, "DEFAULT_ROOTS", ("hear",))
        assert RG.main(["--roots", "hear"]) == 1
        err = capsys.readouterr().err
        assert "hear/m.py:1" in err and "aws" in err

    def test_the_rules_can_be_printed_for_a_reviewer(self, capsys):
        assert RG.main(["--list-rules"]) == 0
        out = capsys.readouterr().out
        for rule in RG.RULES:
            assert rule.name in out
