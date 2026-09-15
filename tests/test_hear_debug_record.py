import argparse
import datetime as dt
import json
import os
import struct

import pytest

from tools import hear_debug_record as DR


def _wav(samples=240000, fs=48000):
    data = b"\x11\x22" * samples
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, fs, fs * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


def _candidate(sample, when_s):
    basename = "nyquist-db21acd5-%d.wav" % sample
    return {
        "clip": "/clips/" + basename,
        "clip_key": "nyquist:db21acd5:%d" % sample,
        "parts": {"basename": basename},
        "anchored": True,
        "ts_utc_s": when_s,
    }


def _args(tmp_path, **overrides):
    values = {
        "grant_id": "grant-123",
        "operator": "operator@example",
        "reason": "debug unexpected impulse",
        "ack": DR.ACK,
        "node": "nyquist",
        "window_start": dt.datetime(2026, 9, 15, 18, 0, tzinfo=dt.timezone.utc),
        "window_end": dt.datetime(2026, 9, 15, 18, 5, tzinfo=dt.timezone.utc),
        "clip_count": 2,
        "ttl_s": 300,
        "byte_cap": 2_000_000,
        "output": str(tmp_path / "audit.json"),
        "timeout": 10.0,
    }
    values.update(overrides)
    return type("Args", (), values)()


class TestRequestGate:
    @pytest.mark.parametrize("field,value,message", [
        ("ack", "yes", "--ack"),
        ("clip_count", 7, "--clip-count"),
        ("ttl_s", 301, "--ttl-s"),
        ("byte_cap", DR.MAX_BYTE_CAP + 1, "--byte-cap"),
        ("reason", "short", "--reason"),
    ])
    def test_rejects_unbounded_or_unauthorized_requests(self, tmp_path, field, value, message):
        with pytest.raises(DR.DebugRecordingError, match=message):
            DR.validate_request(_args(tmp_path, **{field: value}))

    def test_rejects_non_utc_and_overlong_windows(self):
        with pytest.raises(argparse.ArgumentTypeError):
            DR.parse_utc("2026-09-15T18:00:00-04:00")

    def test_rejects_window_over_five_minutes(self, tmp_path):
        with pytest.raises(DR.DebugRecordingError, match="UTC window"):
            DR.validate_request(_args(
                tmp_path,
                window_end=dt.datetime(2026, 9, 15, 18, 5, 1, tzinfo=dt.timezone.utc)))

    def test_cli_requires_every_authorization_field(self):
        with pytest.raises(SystemExit):
            DR.parser().parse_args([])


class TestEphemeralCapture:
    def test_extracts_derived_output_and_proves_raw_deletion(self, tmp_path, monkeypatch):
        when = dt.datetime(2026, 9, 15, 18, 2, tzinfo=dt.timezone.utc).timestamp()
        candidates = [_candidate(100, when), _candidate(200, when + 1)]
        workspaces = []

        monkeypatch.setattr(DR.HD, "fetch_status",
                            lambda ip, timeout: {"node": "nyquist"})
        monkeypatch.setattr(DR, "_candidate_rows",
                            lambda node, ip, start, end, timeout: candidates)
        monkeypatch.setattr(DR.HD, "fetch_clip",
                            lambda ip, name, timeout, max_bytes=None: (_wav(), None))
        real_tempdir = DR.tempfile.TemporaryDirectory

        def tracking_tempdir(*args, **kwargs):
            made = real_tempdir(*args, **kwargs)
            workspaces.append(made.name)
            return made

        monkeypatch.setattr(DR.tempfile, "TemporaryDirectory", tracking_tempdir)
        audit = DR.run(_args(tmp_path))

        assert audit["result"] == "complete"
        assert len(audit["clips"]) == 2
        assert audit["raw_bytes_processed"] == 2 * len(_wav())
        assert audit["clips"][0]["derived"]["sha256"]
        assert audit["clips"][0]["derived"]["sample_count"] == 240000
        assert audit["cleanup"]["raw_files_remaining"] == 0
        assert audit["cleanup"]["workspace_deleted"] is True
        assert all(row["raw_deleted"] for row in audit["cleanup"]["per_clip"])
        assert all(not os.path.exists(path) for path in workspaces)
        assert not list(tmp_path.rglob("*.wav"))

    def test_byte_cap_stops_before_raw_is_written(self, tmp_path, monkeypatch):
        when = dt.datetime(2026, 9, 15, 18, 2, tzinfo=dt.timezone.utc).timestamp()
        candidates = [_candidate(100, when), _candidate(200, when + 1)]
        body = _wav()
        monkeypatch.setattr(DR.HD, "fetch_status",
                            lambda ip, timeout: {"node": "nyquist"})
        monkeypatch.setattr(DR, "_candidate_rows",
                            lambda node, ip, start, end, timeout: candidates)
        def bounded(ip, name, timeout, max_bytes=None):
            return ((None, "byte_cap") if len(body) > max_bytes else (body, None))

        monkeypatch.setattr(DR.HD, "fetch_clip", bounded)

        audit = DR.run(_args(tmp_path, byte_cap=len(body)))

        assert len(audit["clips"]) == 1
        assert audit["raw_bytes_processed"] == len(body)
        assert audit["refusals"] == [{
            "clip": candidates[1]["clip"],
            "reason": "raw_byte_cap",
        }]
        assert audit["cleanup"]["workspace_deleted"] is True

    def test_wrong_node_identity_fails_before_any_clip_fetch(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(DR.HD, "fetch_status", lambda ip, timeout: {"node": "mach"})
        monkeypatch.setattr(DR.HD, "fetch_clip",
                            lambda *args, **kwargs: called.append(args))
        with pytest.raises(DR.DebugRecordingError, match="reported node identity") as raised:
            DR.run(_args(tmp_path))
        assert raised.value.audit["result"] == "failed"
        assert raised.value.audit["cleanup"]["workspace_deleted"] is True
        assert called == []

    def test_malformed_clip_name_is_audited_without_fetching(self, tmp_path, monkeypatch):
        when = dt.datetime(2026, 9, 15, 18, 2, tzinfo=dt.timezone.utc).timestamp()
        candidate = _candidate(100, when)
        candidate.update({"parts": None, "bad_name": "bad clip name"})
        called = []
        monkeypatch.setattr(DR.HD, "fetch_status",
                            lambda ip, timeout: {"node": "nyquist"})
        monkeypatch.setattr(DR, "_candidate_rows",
                            lambda node, ip, start, end, timeout: [candidate])
        monkeypatch.setattr(DR.HD, "fetch_clip",
                            lambda *args, **kwargs: called.append(args))

        audit = DR.run(_args(tmp_path))

        assert called == []
        assert audit["refusals"] == [{
            "clip": candidate["clip"],
            "reason": "invalid_clip_name",
            "detail": "bad clip name",
        }]

    def test_main_writes_only_json_derived_output(self, tmp_path, monkeypatch):
        audit = {
            "result": "complete",
            "request": {"node": "nyquist"},
            "clips": [],
            "raw_bytes_processed": 0,
            "cleanup": {"raw_files_remaining": 0},
        }
        monkeypatch.setattr(DR, "run", lambda args: audit)
        out = tmp_path / "audit.json"
        rc = DR.main([
            "--grant-id", "grant-123",
            "--operator", "operator@example",
            "--reason", "debug unexpected impulse",
            "--ack", DR.ACK,
            "--node", "nyquist",
            "--window-start", "2026-09-15T18:00:00Z",
            "--window-end", "2026-09-15T18:05:00Z",
            "--clip-count", "1",
            "--ttl-s", "300",
            "--byte-cap", "1000000",
            "--output", str(out),
        ])
        assert rc == 0
        assert json.loads(out.read_text())["result"] == "complete"
        assert not list(tmp_path.rglob("*.wav"))

    def test_main_writes_a_failure_audit(self, tmp_path, monkeypatch):
        def fail(_args):
            raise DR.DebugRecordingError("node unavailable")

        monkeypatch.setattr(DR, "run", fail)
        out = tmp_path / "failure.json"
        rc = DR.main([
            "--grant-id", "grant-123",
            "--operator", "operator@example",
            "--reason", "debug unexpected impulse",
            "--ack", DR.ACK,
            "--node", "nyquist",
            "--window-start", "2026-09-15T18:00:00Z",
            "--window-end", "2026-09-15T18:05:00Z",
            "--clip-count", "1",
            "--ttl-s", "300",
            "--byte-cap", "1000000",
            "--output", str(out),
        ])
        assert rc == 2
        failure = json.loads(out.read_text())
        assert failure["result"] == "failed"
        assert failure["authorization"]["grant_id"] == "grant-123"
