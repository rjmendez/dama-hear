"""A dropped connection must not cost a node its scene lane.

⚠️THE FALLBACK IS RIGHT FOR OLD FIRMWARE AND WRONG FOR A RESET. When `/ls` fails, `scene_names()`
falls back to the legacy names -- correct for firmware that predates the endpoint, and on current
firmware it means re-tailing a frozen scene.csv and ingesting nothing while the dated file the
node is really writing goes uncollected. Measured on nyquist 2026-09-09: `/ls` failed on 2 of 4
runs, each time costing that node every scene row for the run, because the ESP32 core serves one
client at a time and REFUSES the rest rather than queueing them.

So these check the distinction the drain could not previously make: retry a transport failure,
never retry an answer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import hear_drain as HD                                  # noqa: E402
import urllib.error                                                 # noqa: E402


class Flaky:
    """`/ls` that fails `n` times with a reset and then answers."""

    def __init__(self, fail_times, exc=None, answer=None):
        self.left = fail_times
        self.exc = exc or ConnectionResetError(104, "Connection reset by peer")
        self.answer = {"scene-20260909.csv": 10} if answer is None else answer
        self.calls = 0

    def __call__(self, ip, timeout=None):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise self.exc
        return self.answer


def _run(monkeypatch, flaky, **kw):
    monkeypatch.setattr(HD, "_ls_sizes", flaky)
    slept = []
    return HD.ls_sizes_retrying("10.0.0.1", sleep=slept.append, **kw), flaky, slept


def test_a_single_reset_is_retried_and_the_listing_survives(monkeypatch):
    (sizes, err, attempts), f, _ = _run(monkeypatch, Flaky(1))
    assert sizes == {"scene-20260909.csv": 10}, "one dropped connection lost the whole listing"
    assert err is None and attempts == 2 and f.calls == 2


def test_the_retry_is_visible_in_the_run_record(monkeypatch):
    # A retry that succeeds silently turns a node with real contention into a healthy-looking one.
    monkeypatch.setattr(HD, "_ls_sizes", Flaky(1))
    monkeypatch.setattr(HD.time, "sleep", lambda s: None)
    _sizes, _err, attempts = HD.ls_sizes_retrying("10.0.0.1")
    assert attempts == 2, "the caller cannot report a retry it is not told about"


def test_it_gives_up_and_still_says_so(monkeypatch):
    (sizes, err, attempts), f, _ = _run(monkeypatch, Flaky(99))
    assert sizes is None, "a total failure must not be dressed up as an empty listing"
    assert "Connection reset" in err
    assert attempts == HD.LS_RETRIES and f.calls == HD.LS_RETRIES


def test_an_http_answer_is_NOT_retried(monkeypatch):
    """⚠️404 IS THE NODE ANSWERING. Old firmware has no /ls, and that is the case the blind
    fallback exists for -- retrying it just hammers a node that already told us the truth."""
    e = urllib.error.HTTPError("http://x/ls", 404, "Not Found", {}, None)
    (sizes, err, attempts), f, _ = _run(monkeypatch, Flaky(99, exc=e))
    assert sizes is None and attempts == 1 and f.calls == 1, "a 404 was retried"


def test_the_backoff_grows_and_does_not_run_after_the_last_try(monkeypatch):
    (_s, _e, _a), _f, slept = _run(monkeypatch, Flaky(99))
    assert slept == [HD.LS_RETRY_BACKOFF_S * 1, HD.LS_RETRY_BACKOFF_S * 2], \
        "a sleep after the final attempt is dead time in a 15-minute budget"


def test_a_clean_first_call_neither_sleeps_nor_retries(monkeypatch):
    (sizes, err, attempts), f, slept = _run(monkeypatch, Flaky(0))
    assert attempts == 1 and f.calls == 1 and slept == []


def test_the_drained_node_keeps_its_dated_scene_file_through_a_reset(monkeypatch, tmp_path):
    """End to end: the reset happens, and the node still drains the file it is actually writing.

    Without the retry this asks for the LEGACY names and collects nothing.
    """
    from hear import pool as P
    from tests.test_scene_discovery import MultiFileNode, _scene_bytes

    n = MultiFileNode({"scene.csv": _scene_bytes(5),
                       "scene-20260909.csv": _scene_bytes(6, start_uptime=900)})
    real_ls, state = n.ls, {"first": True}

    def flaky_ls(ip, timeout=None):
        if state["first"]:
            state["first"] = False
            raise ConnectionResetError(104, "Connection reset by peer")
        return real_ls(ip, timeout)

    monkeypatch.setattr(HD, "fetch_status", n.status)
    monkeypatch.setattr(HD, "fetch_sd", n.sd)
    monkeypatch.setattr(HD, "_ls_sizes", flaky_ls)
    monkeypatch.setattr(HD.time, "sleep", lambda s: None)

    out = HD.drain_node(P.Pool(str(tmp_path / "pool")), n.node, "10.0.0.1")
    assert out["ls_ok"] is True and out.get("ls_retried") == 2
    assert out["scene_live"] == "scene-20260909.csv", "fell back to the legacy names on a reset"
    assert out["scene_added"] > 0
