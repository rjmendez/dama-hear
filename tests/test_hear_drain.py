"""The drain's byte accounting: how far back its scene tail reached, and what it did not reach.

⚠️THE NULL CONTROL COMES FIRST AND IS THE TEST THAT MATTERS. 60.05% of every scene row this pool
has ever read was a duplicate -- the signature of a tail window that comfortably overlaps the last
one. A gap detector that fires on that steady state would fire on every healthy run in production
and would be switched off inside a week, so `TestNullControl` simulates the real thing (a file
growing at the measured row rate, fetched on the CronJob's period, with the same window/growth
ratio the live nodes have) and asserts SILENCE across many runs before any test asserts an alarm.

No HTTP is done here. `fetch_status`, `fetch_sd` and `_ls_sizes` are replaced by a `FakeNode`
holding a scene.csv in memory, which is also what makes the byte arithmetic exactly checkable:
the fixture knows precisely how many bytes it withheld.
"""
import binascii
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import pool as P                                          # noqa: E402
from hear import scenefile as SF                                    # noqa: E402
from tools import hear_drain as HD                                  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


# ---------------------------------------------------------------- fixture scene.csv

def _mel_hex(bands=20, slices=4, seed=1):
    rng = np.random.default_rng(seed)
    q = rng.integers(-128, 127, size=(bands, slices), dtype=np.int8)
    return binascii.hexlify(q.tobytes()).decode()


def _scene_row(gen, utc_us, uptime_s, sample, node="nyquist", seed=1):
    vals = {
        "node": node, "utc_us": str(utc_us), "uptime_s": str(uptime_s), "sample": str(sample),
        "bands": "20", "slices": "4", "span_ms": "1024", "ref_db4": "251",
        "frames": "64", "fft_us": "18987", "mel_hex": _mel_hex(seed=seed),
        "f_lo_hz": "62.5", "f_hi_hz": "7812.5",
    }
    return ",".join(vals[c] for c in gen.written)


class FakeNode:
    """A node whose scene.csv grows, served through the same three calls the drain makes.

    `sd()` reproduces the firmware exactly: `f.seek(size - tail)` and nothing else -- there is no
    offset argument and no Range header, so a bigger `tail=` is the only reach-back the drain has.
    `ls()` reports the CURRENT size, which is what the watermark arithmetic is built on.
    """

    def __init__(self, node="nyquist", gen=SF.S2, boot_epoch_us=1788813341000000,
                 uptime0=100, status=None):
        self.node = node
        self.gen = gen
        self.boot_epoch_us = boot_epoch_us
        self.uptime = uptime0
        self.sample = 0
        self.seed = 0
        self.data = (",".join(gen.declared) + "\n").encode()
        self.rows_written = 0
        self.status_extra = status or {}
        self.sd_calls = []          # (name, tail) for every fetch_sd
        self.ls_calls = 0

    def grow(self, rows):
        """Append `rows` scene rows, one per 1.024 s, as the node does."""
        out = []
        for _ in range(rows):
            self.uptime += 1
            self.sample += 16384
            self.seed += 1
            utc = self.boot_epoch_us + self.uptime * 1_000_000
            out.append(_scene_row(self.gen, utc, self.uptime, self.sample,
                                  node=self.node, seed=self.seed))
        self.data += ("\n".join(out) + "\n").encode()
        self.rows_written += rows
        return self

    def roll(self):
        """What a schema change does: scene.csv restarts, so the file SHRINKS."""
        self.data = (",".join(self.gen.declared) + "\n").encode()
        self.rows_written = 0
        return self

    # ---- the three calls the drain makes
    def status(self, ip, timeout=None):
        st = {"node": self.node, "uptime_s": self.uptime,
              "scene": {"rows": self.rows_written, "written": self.rows_written,
                        "short_blocks": 0, "write_fail": 0},
              "acq": {"fs_clean_hz": 16000.0, "win_s": 300, "drop_s": 0, "drop_samples": 0}}
        st.update(self.status_extra)
        return st

    def sd(self, ip, name, timeout=None, tail=None):
        self.sd_calls.append((name, tail))
        if name != "scene.csv":
            return None                      # scene-prev/dets/health absent, which is normal
        body = self.data
        if tail and len(body) > tail:
            body = body[len(body) - tail:]
        if not body.strip():
            return None
        if tail:
            return body if b"\n" in body else None
        return body

    def ls(self, ip, timeout=None):
        self.ls_calls += 1
        return {"scene.csv": len(self.data)}


class GrowingNode(FakeNode):
    """A node whose scene.csv grows WHILE it is being served, which is what the real one does.

    ⚠️`FakeNode` grows only between drains, and that is exactly the blind spot that let a
    catch-up refetch look correct. A 2 MB tail at the node's measured 40-135 KB/s takes 15-50 s,
    during which the file gains ~3.5-12 KB at ~235 B/s -- and `/sd` seeks against the size AT
    REFETCH TIME (night_node.ino:1985-1986), not against the size the drain read from `/ls`.
    """

    def __init__(self, grow_rows_per_fetch=0, **kw):
        super().__init__(**kw)
        self.grow_rows_per_fetch = grow_rows_per_fetch

    def sd(self, ip, name, timeout=None, tail=None):
        body = super().sd(ip, name, timeout=timeout, tail=tail)
        if name == "scene.csv" and self.grow_rows_per_fetch:
            self.grow(self.grow_rows_per_fetch)
        return body


def _rows_in_pool(pl, n):
    """Every `utc_us` the pool holds for this node -- the ground truth a byte count is a proxy for."""
    return {int(r["utc_us"]) for r in pl.scene(node=n.node) if r.get("utc_us")}


def _rows_on_card(n, uptime0=100):
    return {n.boot_epoch_us + up * 1_000_000 for up in range(uptime0 + 1, n.uptime + 1)}


@pytest.fixture
def wired(monkeypatch):
    """Install a FakeNode over the drain's three network calls and hand back a builder."""
    def build(cls=FakeNode, **kw):
        n = cls(**kw)
        monkeypatch.setattr(HD, "fetch_status", n.status)
        monkeypatch.setattr(HD, "fetch_sd", n.sd)
        monkeypatch.setattr(HD, "_ls_sizes", n.ls)
        return n
    return build


def _drain(pl, n, tail, **kw):
    """One run with a stated tail size, so a test can use a window it can reason about."""
    HD.SCENE_TAIL_BYTES, old = tail, HD.SCENE_TAIL_BYTES
    try:
        return HD.drain_node(pl, n.node, "10.0.0.1", **kw)
    finally:
        HD.SCENE_TAIL_BYTES = old


def _row_bytes(n):
    """The fixture's own row size, measured the way the drain measures it."""
    return HD.mean_row_bytes(n.data)


# ---------------------------------------------------------------- the null control

class TestNullControl:
    """Normal operation must be silent. This is the test the alarm has to survive."""

    def test_a_file_growing_at_the_measured_rate_never_reports_a_gap(self, tmp_path, wired):
        # The live ratio: nyquist writes ~235 B/s and the CronJob fires every 15 min, so each run
        # adds ~211 KB against a 2 MB window -- about a tenth of it. Scaled down by 100x here so
        # the same overlap is exercised without building a 2 MB fixture 20 times.
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        tail = 20_000
        seen = []
        for _ in range(20):
            r = _drain(pl, n, tail)
            assert r["ok"], r["errors"]
            seen.append(r["unfetched_bytes"])
            n.grow(9)                                   # ~1/10th of the window, as measured
        assert seen == [0] * 20, seen
        assert all(r["unfetched_unknown"] is False for _ in [0])
        # and every one of those runs measured exactly one scene tail -- no second fetch
        assert [c for c in n.sd_calls if c[0] == "scene.csv"] == [("scene.csv", tail)] * 20

    def test_the_steady_state_really_is_mostly_duplicate(self, tmp_path, wired):
        # If the fixture did NOT overlap heavily it would not be reproducing production, and the
        # silence above would prove nothing.
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        _drain(pl, n, 20_000)
        n.grow(9)
        r = _drain(pl, n, 20_000)
        f = [f for f in r["files"] if f["name"] == "scene.csv"][0]
        assert f["duplicate"] > f["added"] * 3, f


# ---------------------------------------------------------------- the arithmetic itself

class TestSceneGap:
    """`scene_gap` is pure, so the byte-exact boundaries are checked here rather than through a
    fixture that can only grow in whole rows."""

    def test_touching_windows_are_not_a_gap(self):
        # prev window ended at 1000, this one starts exactly there.
        g = HD.scene_gap(size_now=1000 + 500, fetched_bytes=500, prev_size=1000, row_bytes=235.0)
        assert g["window_start"] == 1000
        assert g["raw_gap_bytes"] == 0 and g["unfetched_bytes"] == 0

    def test_overlapping_windows_clamp_rather_than_going_negative(self):
        g = HD.scene_gap(size_now=1000 + 499, fetched_bytes=500, prev_size=1000, row_bytes=235.0)
        assert g["raw_gap_bytes"] == -1
        assert g["unfetched_bytes"] == 0

    def test_a_whole_row_missed_is_reported_exactly(self):
        g = HD.scene_gap(size_now=10_000, fetched_bytes=500, prev_size=8_000, row_bytes=235.0)
        assert g["window_start"] == 9_500
        assert g["unfetched_bytes"] == 1_500
        assert g["unfetched_rows_est"] == round(1500 / 235.0)

    def test_a_missing_size_is_unknown_and_is_not_zero(self):
        g = HD.scene_gap(size_now=None, fetched_bytes=500, prev_size=8_000, row_bytes=235.0)
        assert g["size_unknown"] is True
        assert g["unfetched_bytes"] is None          # ⚠️not 0: absent must not read as clean

    def test_the_first_ever_run_is_not_loss(self):
        g = HD.scene_gap(size_now=16_603_235, fetched_bytes=2_000_000, prev_size=None,
                         row_bytes=235.0)
        assert g["first_run"] is True
        assert g["unfetched_bytes"] == 0

    def test_a_shrunken_file_is_a_roll_not_a_negative_gap(self):
        g = HD.scene_gap(size_now=4_000, fetched_bytes=4_000, prev_size=16_603_235,
                         row_bytes=235.0)
        assert g["rolled"] is True
        assert g["unfetched_bytes"] == 0


class TestSubRowResidual:
    """The threshold is one row, and one row is MEASURED off the fetched body -- never pinned.

    S1 and S2 rows are different sizes and both are in this pool's real ledger, so the constant
    is validated on a generation other than the one it was derived from.
    """

    def _bodies(self):
        s2 = FakeNode(gen=SF.S2).grow(40).data
        s1 = FakeNode(gen=SF.S1).grow(40).data
        return s1, s2

    def test_the_two_generations_really_do_have_different_row_sizes(self):
        s1, s2 = self._bodies()
        assert HD.mean_row_bytes(s1) < HD.mean_row_bytes(s2)

    def test_each_generation_clamps_against_its_own_row_size(self):
        s1, s2 = self._bodies()
        r1, r2 = HD.mean_row_bytes(s1), HD.mean_row_bytes(s2)
        for rb in (r1, r2):
            below = HD.scene_gap(10_000, 500, 9_500 - int(rb) + 1, rb)
            assert below["sub_row"] is True and below["unfetched_bytes"] == 0
            at = HD.scene_gap(10_000, 500, 9_500 - int(rb) - 1, rb)
            assert at["unfetched_bytes"] == int(rb) + 1

    def test_the_s2_threshold_is_wrong_for_s1_which_is_why_it_is_not_a_constant(self):
        # A gap of exactly one S1 row is real loss for an S1 node, and an S2-sized threshold
        # would swallow it. This is the failure a hardcoded 235 would have.
        s1, s2 = self._bodies()
        r1, r2 = HD.mean_row_bytes(s1), HD.mean_row_bytes(s2)   # 220.6 and 228.5 here; 226 and
        gap = int(r1) + 1                                       # 235 in the real ledger
        assert r1 <= gap < r2
        assert HD.scene_gap(10_000, 500, 9_500 - gap, r1)["unfetched_bytes"] == gap
        assert HD.scene_gap(10_000, 500, 9_500 - gap, r2)["unfetched_bytes"] == 0

    def test_a_body_with_no_complete_row_has_no_measurable_row_size(self):
        assert HD.mean_row_bytes(b"no newline here") is None


# ---------------------------------------------------------------- end to end

class TestGapThroughTheDrain:
    def test_a_withheld_stretch_is_reported_to_the_byte(self, tmp_path, wired):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        tail = 20_000
        _drain(pl, n, tail)
        watermark = HD.read_watermarks(pl.root)["nyquist"]["scene.csv"]["size"]
        assert watermark == len(n.data)

        n.grow(400)                                     # far more than the window covers
        size_now = len(n.data)
        r = _drain(pl, n, tail)
        withheld = size_now - tail - watermark
        assert withheld > 0
        g = [f for f in r["files"] if f["name"] == "scene.csv"][0]["gap"]
        assert g["unfetched_bytes"] == withheld
        assert r["unfetched_bytes"] == withheld
        est = g["unfetched_rows_est"]
        assert abs(est - withheld / _row_bytes(n)) <= 1

    def test_the_first_run_against_a_16mb_file_reports_no_loss(self, tmp_path, wired):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(400)
        r = _drain(pl, n, 20_000)
        g = [f for f in r["files"] if f["name"] == "scene.csv"][0]["gap"]
        assert g["first_run"] is True and g["unfetched_bytes"] == 0
        assert r["unfetched_bytes"] == 0

    def test_a_roll_resets_the_watermark_instead_of_reporting_the_whole_file(self, tmp_path,
                                                                            wired):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(300)
        _drain(pl, n, 20_000)
        big = HD.read_watermarks(pl.root)["nyquist"]["scene.csv"]["size"]
        n.roll().grow(5)
        r = _drain(pl, n, 20_000)
        g = [f for f in r["files"] if f["name"] == "scene.csv"][0]["gap"]
        assert g["rolled"] is True
        assert g["unfetched_bytes"] == 0
        after = HD.read_watermarks(pl.root)["nyquist"]["scene.csv"]["size"]
        assert after == len(n.data) and after < big

    def test_the_watermark_does_not_advance_when_the_ingest_fails(self, tmp_path, wired,
                                                                  monkeypatch):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        _drain(pl, n, 20_000)
        before = HD.read_watermarks(pl.root)["nyquist"]["scene.csv"]["size"]

        n.grow(400)

        def boom(*a, **kw):
            raise RuntimeError("gzip write failed")
        monkeypatch.setattr(pl, "ingest_scene", boom)
        r = _drain(pl, n, 20_000)
        assert not r["ok"] and any("ingest" in e for e in r["errors"])
        after = HD.read_watermarks(pl.root)["nyquist"]["scene.csv"]["size"]
        assert after == before, "a failed run must leave the next one still able to see the gap"


class TestThereIsNoCatchUp:
    """⚠️THE DRAIN MEASURES THE GAP AND DOES NOT TRY TO CLOSE IT, AND THAT IS DELIBERATE.

    A refetch has to size a bigger `tail=` against a size read BEFORE the first body, but the node
    seeks against the size at REFETCH time (night_node.ino:1985-1986) and the file grew for the
    whole of the first fetch. The refetch therefore lands forward of the gap. Worse, the residual
    was recomputed against the same stale size, so a refetch that missed reported success. These
    tests are the guard on that decision: one scene fetch per run, and an honest number.
    """

    def test_only_one_scene_fetch_is_ever_issued(self, tmp_path, wired):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        tail = 20_000
        _drain(pl, n, tail)
        n.grow(400)                                     # far more than the window covers
        n.sd_calls.clear()
        r = _drain(pl, n, tail)
        scene = [c for c in n.sd_calls if c[0] == "scene.csv"]
        assert scene == [("scene.csv", tail)], scene
        assert r["unfetched_bytes"] > 0                 # named, not closed

    def test_a_refetch_would_have_under_reached_and_then_reported_success(self, tmp_path, wired):
        """The measurement that removed the catch-up, kept as the reason it stays removed.

        `grow_rows_per_fetch` stands in for the 15-50 s a 2 MB tail takes on the real node. A
        window sized against the pre-fetch `size_now` -- which is all a refetch could do -- does
        not reach the gap, and re-running `scene_gap` against that same stale `size_now` scores
        it as covered.
        """
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired(cls=GrowingNode, grow_rows_per_fetch=20)
        n.grow(60)
        tail = 20_000
        _drain(pl, n, tail)
        n.grow(400)

        size_now = len(n.data)                          # what /ls would report, read BEFORE the body
        prev = HD.read_watermarks(pl.root)["nyquist"]["scene.csv"]["size"]
        body = HD.fetch_sd("10.0.0.1", "scene.csv", tail=tail)
        gap = HD.scene_gap(size_now, len(body), prev, HD.mean_row_bytes(body))
        assert gap["unfetched_bytes"] > 0

        # the refetch a catch-up would have made: fetched + gap + one row, as it was written
        wanted = int(len(body) + gap["unfetched_bytes"] + gap["mean_row_bytes"] + 1)
        bigger = HD.fetch_sd("10.0.0.1", "scene.csv", tail=wanted)
        first_byte_actually_served = len(n.data) - len(bigger)
        # ⚠️the node seeked against a file that had grown, so the second window starts FORWARD of
        # where the gap is -- it never reached back past `prev`.
        assert first_byte_actually_served > prev, (first_byte_actually_served, prev)
        # ...and the arithmetic the catch-up used, against the stale size, calls that covered
        stale = HD.scene_gap(size_now, len(bigger), prev, HD.mean_row_bytes(bigger))
        assert stale["unfetched_bytes"] == 0, "this is the false clean the refetch reported"

    def test_the_gap_stays_honest_when_the_file_grows_during_the_fetch(self, tmp_path, wired):
        """The single fetch's number is a LOWER bound on the loss, never a false zero.

        `size_now` is read before the body, so growth during the fetch understates `window_start`
        and therefore the gap. The direction is the safe one and it is asserted here rather than
        assumed: what the drain reports must not exceed what is genuinely missing.
        """
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired(cls=GrowingNode, grow_rows_per_fetch=20)
        n.grow(60)
        _drain(pl, n, 20_000)
        n.grow(400)
        r = _drain(pl, n, 20_000)
        missing = _rows_on_card(n, uptime0=100) - _rows_in_pool(pl, n)
        assert missing, "the fixture must actually withhold rows or this proves nothing"
        reported_rows = [f for f in r["files"]
                         if f["name"] == "scene.csv"][0]["gap"]["unfetched_rows_est"]
        assert 0 < reported_rows <= len(missing), (reported_rows, len(missing))


class TestLsFailure:
    def test_an_unreachable_ls_reads_as_unknown_and_never_as_clean(self, tmp_path, wired,
                                                                   monkeypatch):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)

        def boom(*a, **kw):
            raise OSError("connection refused")
        monkeypatch.setattr(HD, "_ls_sizes", boom)
        r = _drain(pl, n, 20_000)
        g = [f for f in r["files"] if f["name"] == "scene.csv"][0]["gap"]
        assert g["size_unknown"] is True
        assert g["unfetched_bytes"] is None
        assert r["unfetched_unknown"] is True
        # ⚠️None, NOT 0. Nothing was measured, and this module's whole contract is that an
        # unmeasured thing must not render as a clean one.
        assert r["unfetched_bytes"] is None
        assert "/ls gave no size" in r["unfetched_reason"]
        assert r["ls_ok"] is False and "connection refused" in r["ls_error"]
        # ⚠️and the run is still OK: the fetch and the ingest worked. Failing it here would mark
        # firmware without /ls permanently broken, and then STALE, while its data flowed fine.
        assert r["ok"] is True and r["scene_added"] > 0
        # and no watermark is written off a size nobody measured
        assert HD.read_watermarks(pl.root).get("nyquist", {}).get("scene.csv") is None

    def test_a_file_missing_from_the_listing_is_also_unknown(self, tmp_path, wired, monkeypatch):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(50)
        monkeypatch.setattr(HD, "_ls_sizes", lambda *a, **kw: {"dets.csv": 4557})
        r = _drain(pl, n, 20_000)
        g = [f for f in r["files"] if f["name"] == "scene.csv"][0]["gap"]
        assert g["size_unknown"] is True and g["unfetched_bytes"] is None


class TestLsParsing:
    """Against the bytes a real node served, not against a hand-written line."""

    @pytest.mark.parametrize("name,expect", [("ls_nyquist.txt", 16_603_235 // 1),
                                             ("ls_mach.txt", None)])
    def test_it_reads_the_captured_listings(self, name, expect, monkeypatch):
        raw = open(os.path.join(FIXTURES, name), "rb").read()
        monkeypatch.setattr(HD, "_get", lambda url, timeout=None: raw)
        sizes = HD._ls_sizes("10.0.0.1")
        assert "scene.csv" in sizes and sizes["scene.csv"] > 10_000_000
        assert "health.csv" in sizes and "dets.csv" in sizes
        assert "clips" not in sizes            # `d ` is a directory, not a file
        assert all(isinstance(v, int) for v in sizes.values())

    def test_the_file_sizes_the_module_quotes_are_the_ones_that_were_captured(self, monkeypatch):
        """The reach-back arithmetic in the module docstring is quoted against these listings.

        ⚠️A constant that is validated against nothing drifts silently, which is how the tail's
        reach-back came to be described as 2.3 h long after it stopped being that.
        """
        sizes = {}
        for name in ("nyquist", "mach"):
            body = open(os.path.join(FIXTURES, "ls_%s.txt" % name), "rb").read()
            monkeypatch.setattr(HD, "_get", lambda *a, **kw: body)
            sizes[name] = HD._ls_sizes("10.0.0.1")["scene.csv"]
        assert sizes == {"nyquist": 16_771_742, "mach": 16_435_916}
        assert "16,771,742 B" in HD.__doc__ or "16,771,742 B" in open(HD.__file__).read()
        # 2 MB / 235.0 B per row / (1 row per 1.024 s), the figure the docstring states
        assert abs(HD.SCENE_TAIL_BYTES / 235.0 * 1.024 / 3600.0 - 2.42) < 0.01
        assert abs(HD.SCENE_TAIL_BYTES / 232.1 * 1.024 / 3600.0 - 2.45) < 0.01
        # and the window really is a small fraction of the file it is cut from
        assert HD.SCENE_TAIL_BYTES < min(sizes.values()) / 5

    def test_a_leading_slash_on_the_name_is_normalised(self, monkeypatch):
        monkeypatch.setattr(HD, "_get",
                            lambda url, timeout=None: b"- /scene.csv  1234 B\n")
        assert HD._ls_sizes("10.0.0.1") == {"scene.csv": 1234}

    def test_an_unparseable_line_is_dropped_rather_than_guessed(self, monkeypatch):
        monkeypatch.setattr(HD, "_get",
                            lambda url, timeout=None: b"- scene.csv  wat B\n- dets.csv  12 B\n")
        assert HD._ls_sizes("10.0.0.1") == {"dets.csv": 12}


# ---------------------------------------------------------------- what /status is called

class TestStatusKeyNames:
    """⚠️Pinned against BOTH live nodes. Every one of these read back as None would look like a
    node with nothing to say instead of a reader with the wrong spelling."""

    @pytest.mark.parametrize("fx", ["status_nyquist.json", "status_mach.json"])
    def test_the_audit_block_reads_the_names_the_firmware_writes(self, fx):
        st = json.load(open(os.path.join(FIXTURES, fx)))
        a = HD.status_audit(st)
        assert a["uptime_s"] is not None
        for k in ("rows", "written", "short_blocks", "write_fail"):
            assert a["scene"][k] is not None, k
        for k in ("fs_clean_hz", "win_s", "drop_s", "drop_samples"):
            assert a["acq"][k] is not None, k

    @pytest.mark.parametrize("fx", ["status_nyquist.json", "status_mach.json"])
    def test_the_names_the_design_used_do_not_exist(self, fx):
        st = json.load(open(os.path.join(FIXTURES, fx)))
        assert "drop_seconds" not in st["acq"]
        assert "fs_clean" not in st["acq"]
        assert "rows" not in st                      # it is scene.rows, not a top-level rows

    @pytest.mark.parametrize("fx", ["status_nyquist.json", "status_mach.json"])
    def test_short_blocks_is_not_the_counter_for_these_gaps(self, fx):
        # Captured live: short_blocks 0 on both nodes while acq.drop_s is 62 s and 89 s.
        st = json.load(open(os.path.join(FIXTURES, fx)))
        assert st["scene"]["short_blocks"] == 0
        assert st["acq"]["drop_s"] > 0

    def test_a_status_missing_the_blocks_gives_nones_rather_than_raising(self):
        a = HD.status_audit({"node": "puc"})
        assert a["scene"]["rows"] is None and a["acq"]["drop_s"] is None


# ---------------------------------------------------------------- the ledger

class TestLedgerInvariants:
    def _entries(self, pl):
        return [json.loads(l) for l in open(pl.ledger_path)
                if json.loads(l).get("kind") == "scene.csv"]

    def test_every_entry_totals_its_own_losses(self, tmp_path, wired):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        _drain(pl, n, 20_000)
        n.grow(400)
        _drain(pl, n, 20_000)
        entries = self._entries(pl)
        assert len(entries) == 2
        for e in entries:
            assert e["rows"] == e["added"] + e["duplicate"] + e["skipped"], e
            assert sum(e["skip_reasons"].values()) == e["skipped"], e

    def test_the_fetch_audit_is_its_own_key_and_not_a_skip_reason(self, tmp_path, wired):
        # ⚠️`skipped` is len(read.skips)+sum(decode errors) and `skip_reasons` is the breakdown
        # that must total it. A byte-accounting key filed in there would make one counter mean
        # two things -- which is the bug class this whole change exists to close.
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        _drain(pl, n, 20_000)
        e = self._entries(pl)[0]
        assert "fetch_audit" in e
        assert e["fetch_audit"]["gap"]["size_now"] == len(n.data)
        assert e["fetch_audit"]["status"]["acq"]["fs_clean_hz"] == 16000.0
        for forbidden in ("fetch_audit", "unfetched_bytes", "never_fetched", "gap"):
            assert forbidden not in e["skip_reasons"]

    def test_an_ingest_without_a_fetch_audit_writes_no_such_key(self, tmp_path, wired):
        # All 54 pre-existing ledger entries lack it; a reader must tolerate absence, and the
        # writer must not invent an empty one that reads as "measured, found nothing".
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(5)
        p = tmp_path / "scene.csv"
        p.write_bytes(n.data)
        e = pl.ingest_scene(str(p), default_node="nyquist")
        assert "fetch_audit" not in e


# ---------------------------------------------------------------- the boot cross-check

class TestBootAudit:
    """Advisory, and it must not turn unanchored rows into phantom loss."""

    def _pool_with(self, tmp_path, rows, node="nyquist"):
        """rows: [(utc_us, uptime_s)]; utc_us 0 means the node had no PPS lock yet."""
        pl = P.Pool(str(tmp_path / "pool"))
        lines = [",".join(SF.S2.declared)]
        for i, (utc, up) in enumerate(rows):
            lines.append(_scene_row(SF.S2, utc, up, 16384 * (i + 1), node=node, seed=i + 1))
        p = tmp_path / "scene.csv"
        p.write_text("\n".join(lines) + "\n")
        pl.ingest_scene(str(p), default_node=node)
        return pl

    def test_it_counts_only_the_boot_it_was_asked_about(self, tmp_path):
        boot_a, boot_b = 1_788_800_000, 1_788_900_000
        rows = [(int((boot_a + u) * 1e6), u) for u in range(100, 140)] \
            + [(int((boot_b + u) * 1e6), u) for u in range(100, 110)]
        pl = self._pool_with(tmp_path, rows)
        st = {"uptime_s": 139, "scene": {"rows": 40, "written": 40}}
        a = HD.boot_audit(pl, "nyquist", st, now=boot_a + 139)
        assert a["ingested_this_boot"] == 40
        assert a["outstanding_max"] == 0

    def test_a_hole_in_a_boot_shows_up_against_what_the_node_wrote(self, tmp_path):
        boot_a = 1_788_800_000
        kept = list(range(100, 130)) + list(range(150, 170))     # 20 rows punched out
        rows = [(int((boot_a + u) * 1e6), u) for u in kept]
        pl = self._pool_with(tmp_path, rows)
        st = {"uptime_s": 169, "scene": {"rows": 70, "written": 70}}
        a = HD.boot_audit(pl, "nyquist", st, now=boot_a + 169)
        assert a["ingested_this_boot"] == 50
        assert a["outstanding_max"] == 20
        assert a["outstanding_min"] == 20                # nothing unanchored to explain it away

    def test_unanchored_rows_are_bounded_not_counted_as_loss(self, tmp_path):
        # ⚠️7.6% of real scene rows carry utc_us == 0 and cannot be clustered on uptime. Guessing
        # a boot for them would report ~7.6% phantom loss, so they widen a RANGE instead.
        boot_a = 1_788_800_000
        rows = [(int((boot_a + u) * 1e6), u) for u in range(100, 130)] \
            + [(0, u) for u in range(10, 20)]
        pl = self._pool_with(tmp_path, rows)
        st = {"uptime_s": 129, "scene": {"rows": 40, "written": 40}}
        a = HD.boot_audit(pl, "nyquist", st, now=boot_a + 129)
        assert a["ingested_this_boot"] == 30
        assert a["unanchored_rows_stored"] == 10
        assert a["unanchored_attributable_max"] == 10
        assert a["outstanding_max"] == 10
        assert a["outstanding_min"] == 0                # they may all belong to this boot

    def test_unanchored_rows_from_OTHER_boots_do_not_widen_this_boots_range(self, tmp_path):
        """⚠️THE LOWER BOUND USED TO COUNT EVERY UNANCHORED ROW THE NODE EVER PRODUCED.

        On the exported corpus that is 2,896 rows for mach across only 786 distinct `uptime_s`
        values (13-808 s, the cold-start window before PPS lock) -- roughly 3.7 boots' worth,
        all subtracted from ONE boot's outstanding count. Within a boot `uptime_s` is strictly
        increasing (rows are 1.024 s apart), so this boot can hold at most one unanchored row per
        distinct value. Here four boots each contribute the same ten `uptime_s` values: 40 rows
        stored, but at most 10 of them can be this boot's.
        """
        boot_a = 1_788_800_000
        rows = [(int((boot_a + u) * 1e6), u) for u in range(100, 130)]
        for _boot in range(4):
            rows += [(0, u) for u in range(10, 20)]
        pl = self._pool_with(tmp_path, rows)
        st = {"uptime_s": 129, "scene": {"rows": 45, "written": 45}}
        a = HD.boot_audit(pl, "nyquist", st, now=boot_a + 129)
        assert a["ingested_this_boot"] == 30
        assert a["unanchored_rows_stored"] == 40       # what the raw walk used to subtract
        assert a["unanchored_attributable_max"] == 10  # what one boot can actually hold
        assert a["outstanding_max"] == 15
        assert a["outstanding_min"] == 5               # 15 - 10, not max(0, 15 - 40) == 0

    def test_an_unanchored_row_above_the_current_uptime_cannot_be_this_boots(self, tmp_path):
        boot_a = 1_788_800_000
        rows = [(int((boot_a + u) * 1e6), u) for u in range(100, 130)] \
            + [(0, u) for u in (10, 11, 12, 900, 901)]   # 900/901 postdate this boot's uptime
        pl = self._pool_with(tmp_path, rows)
        st = {"uptime_s": 129, "scene": {"rows": 40, "written": 40}}
        a = HD.boot_audit(pl, "nyquist", st, now=boot_a + 129)
        assert a["unanchored_rows_stored"] == 5
        assert a["unanchored_attributable_max"] == 3
        assert a["outstanding_min"] == 7                # 10 - 3, not 10 - 5

    def test_it_says_when_it_declined_rather_than_returning_a_clean_zero(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        a = HD.boot_audit(pl, "nyquist", {"scene": {"rows": 1, "written": 1}}, now=1e9)
        assert "skipped" in a and "ingested_this_boot" not in a
        long_up = HD.BOOT_AUDIT_MAX_DAYS * 86400 + 1
        b = HD.boot_audit(pl, "nyquist", {"uptime_s": long_up, "scene": {}}, now=1e9)
        assert "skipped" in b and "ingested_this_boot" not in b

    def test_it_carries_what_the_node_produced_but_did_not_write(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        a = HD.boot_audit(pl, "nyquist", {"uptime_s": 100,
                                          "scene": {"rows": 500, "written": 497}}, now=1e9)
        assert a["not_written"] == 3
        assert a["advisory"] is True


# ---------------------------------------------------------------- --check

class TestCheck:
    def _hb(self, root, **sensor):
        os.makedirs(root, exist_ok=True)
        json.dump({"sensors": {"nyquist": sensor}}, open(HD.heartbeat_path(root), "w"))

    def test_it_fails_on_bytes_nobody_fetched_even_though_the_run_was_fresh(self, tmp_path):
        root = str(tmp_path)
        self._hb(root, kind="node", last_success_s=1000.0, last_unfetched_bytes=735_000)
        code, lines = HD.check(root, max_stale_s=7200.0, now=1010.0)
        assert code == 1
        assert "UNFETCHED" in lines[0] and "735000" in lines[0]

    def test_zero_passes(self, tmp_path):
        root = str(tmp_path)
        self._hb(root, kind="node", last_success_s=1000.0, last_unfetched_bytes=0)
        code, lines = HD.check(root, max_stale_s=7200.0, now=1010.0)
        assert code == 0 and "UNFETCHED" not in lines[0]

    def test_staleness_still_fails_as_it_did(self, tmp_path):
        root = str(tmp_path)
        self._hb(root, kind="node", last_success_s=1000.0, last_unfetched_bytes=0)
        code, lines = HD.check(root, max_stale_s=100.0, now=100_000.0)
        assert code == 1 and "STALE" in lines[0]

    def test_a_heartbeat_written_before_this_existed_is_tolerated(self, tmp_path):
        # Every heartbeat and all 54 ledger entries predating this change lack the field.
        root = str(tmp_path)
        self._hb(root, kind="node", last_success_s=1000.0)
        code, lines = HD.check(root, max_stale_s=7200.0, now=1010.0)
        assert code == 0
        assert "n/a" in lines[0]

    def test_an_unknown_measurement_is_printed_as_unknown_and_not_as_zero(self, tmp_path):
        root = str(tmp_path)
        self._hb(root, kind="node", last_success_s=1000.0, last_unfetched_bytes=None)
        code, lines = HD.check(root, max_stale_s=7200.0, now=1010.0)
        assert "UNKNOWN" in lines[0]
        assert "UNFETCHED" not in lines[0]

    def test_the_threshold_is_settable(self, tmp_path):
        root = str(tmp_path)
        self._hb(root, kind="node", last_success_s=1000.0, last_unfetched_bytes=500)
        assert HD.check(root, now=1010.0, max_unfetched_bytes=1000)[0] == 0
        assert HD.check(root, now=1010.0, max_unfetched_bytes=100)[0] == 1


class TestHeartbeatCarriesTheLoss:
    def test_a_run_that_lost_rows_records_it_even_though_it_also_errored(self, tmp_path, wired,
                                                                        monkeypatch):
        # ⚠️The run that loses rows is often the same run that reports an error, so filing the
        # loss under `if r["ok"]` would hide it in the one case it exists for.
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        _drain(pl, n, 20_000)
        n.grow(600)
        real = n.sd

        def flaky(ip, name, timeout=None, tail=None):
            if name == "dets.csv":
                raise OSError("connection reset")
            return real(ip, name, timeout=timeout, tail=tail)
        monkeypatch.setattr(HD, "fetch_sd", flaky)
        r = _drain(pl, n, 20_000)
        assert not r["ok"]
        hb = HD.write_heartbeat(pl.root, [r], None, now=1000.0)
        assert hb["sensors"]["nyquist"]["last_unfetched_bytes"] == r["unfetched_bytes"] > 0
        code, lines = HD.check(pl.root, max_stale_s=7200.0, now=1010.0)
        assert code == 1 and "UNFETCHED" in lines[0]

    def test_a_measured_gap_survives_an_ingest_that_raises(self, tmp_path, wired, monkeypatch):
        """⚠️THE GAP IS A PROPERTY OF THE FETCH, NOT OF THE STORE.

        The accounting used to be booked after `ingest_scene`, so the `except: continue` that
        handles a failed store took the measurement with it: a run that measured a real gap and
        then failed to write reported 0 unfetched bytes. That is a measured loss rendered as
        clean, in the module whose contract is that it must not be.
        """
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        tail = 20_000
        _drain(pl, n, tail)
        watermark = HD.read_watermarks(pl.root)["nyquist"]["scene.csv"]["size"]
        n.grow(400)
        withheld = len(n.data) - tail - watermark
        assert withheld > 0

        def boom(*a, **kw):
            raise RuntimeError("gzip write failed")
        monkeypatch.setattr(pl, "ingest_scene", boom)
        r = _drain(pl, n, tail)
        assert any("ingest" in e for e in r["errors"])
        assert r["unfetched_unknown"] is False
        assert r["unfetched_bytes"] == withheld
        hb = HD.write_heartbeat(pl.root, [r], None, now=1000.0)
        assert hb["sensors"]["nyquist"]["last_unfetched_bytes"] == withheld
        assert HD.check(pl.root, max_stale_s=7200.0, now=1010.0)[0] == 1

    def test_an_unknown_measurement_reaches_the_heartbeat_as_none(self, tmp_path, wired,
                                                                  monkeypatch):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(50)
        monkeypatch.setattr(HD, "_ls_sizes", lambda *a, **kw: (_ for _ in ()).throw(OSError("x")))
        r = _drain(pl, n, 20_000)
        hb = HD.write_heartbeat(pl.root, [r], None, now=1000.0)
        assert hb["sensors"]["nyquist"]["last_unfetched_bytes"] is None


class TestARunThatMeasuredNothingSaysSo:
    """⚠️THE EARLY RETURNS USED TO REPORT A MEASURED ZERO FOR A RUN THAT MEASURED NOTHING."""

    def test_an_unreachable_status_is_unknown_not_a_clean_zero(self, tmp_path, monkeypatch):
        pl = P.Pool(str(tmp_path / "pool"))

        def boom(*a, **kw):
            raise OSError("no route to host")
        monkeypatch.setattr(HD, "fetch_status", boom)
        r = HD.drain_node(pl, "nyquist", "10.0.0.1")
        assert r["unfetched_bytes"] is None
        assert r["unfetched_unknown"] is True
        assert "/status" in r["unfetched_reason"]
        hb = HD.write_heartbeat(pl.root, [r], None, now=1000.0)
        assert hb["sensors"]["nyquist"]["last_unfetched_bytes"] is None
        code, lines = HD.check(pl.root, max_stale_s=7200.0, now=1010.0)
        assert "UNKNOWN" in lines[0] and "UNFETCHED" not in lines[0]

    def test_a_refused_identity_is_unknown_not_a_clean_zero(self, tmp_path, wired):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired(node="mach")                          # answers as mach, asked for nyquist
        n.grow(50)
        r = HD.drain_node(pl, "nyquist", "10.0.0.1")
        assert any("identity" in e for e in r["errors"])
        assert r["unfetched_bytes"] is None
        assert r["unfetched_unknown"] is True
        assert "mach" in r["unfetched_reason"]
        hb = HD.write_heartbeat(pl.root, [r], None, now=1000.0)
        assert hb["sensors"]["nyquist"]["last_unfetched_bytes"] is None

    def test_a_node_that_serves_no_scene_is_unknown_not_a_clean_zero(self, tmp_path, wired,
                                                                     monkeypatch):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(50)
        monkeypatch.setattr(HD, "fetch_sd", lambda *a, **kw: None)
        r = HD.drain_node(pl, "nyquist", "10.0.0.1")
        assert r["unfetched_bytes"] is None and r["unfetched_unknown"] is True


class TestTheCheckSeesEveryDrain:
    """⚠️THE DRAIN RUNS 4x AS OFTEN AS THE CHECK, so a field the drain overwrites is a field the
    gate mostly never reads. deploy/k8s/hear-drain.yaml: drain `*/15 * * * *`, check `17 * * * *`.
    """

    def test_the_two_cronjobs_really_are_that_far_apart(self):
        import re
        y = open(os.path.join(os.path.dirname(FIXTURES), "..", "deploy", "k8s",
                              "hear-drain.yaml")).read()
        schedules = re.findall(r'^\s*schedule:\s*"([^"]+)"', y, re.M)
        assert schedules == ["*/15 * * * *", "17 * * * *"], schedules
        # 4 drains land between two checks, so the window the check sums must cover at least that
        assert HD.DEFAULT_UNFETCHED_WINDOW_S >= 4 * 15 * 60

    def test_a_loss_three_runs_ago_is_still_seen_by_the_hourly_check(self, tmp_path, wired):
        """The failure this fixes: a gap at :02, and three clean drains before the check at :17."""
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(200)
        _drain(pl, n, 20_000)
        n.grow(400)
        lossy = _drain(pl, n, 20_000)                   # :02 -- the run that skipped bytes
        assert lossy["unfetched_bytes"] > 0
        HD.write_heartbeat(pl.root, [lossy], None, now=120.0)
        for i, t in enumerate((900.0, 1800.0, 2700.0)):  # :15, :30, :45 -- all clean
            n.grow(9)
            clean = _drain(pl, n, 20_000)
            assert clean["unfetched_bytes"] == 0
            HD.write_heartbeat(pl.root, [clean], None, now=t)
        hb = json.load(open(HD.heartbeat_path(pl.root)))
        assert hb["sensors"]["nyquist"]["last_unfetched_bytes"] == 0   # the overwritten field
        code, lines = HD.check(pl.root, max_stale_s=7200.0, now=3600.0)
        assert code == 1, lines
        assert "UNFETCHED" in lines[0] and str(lossy["unfetched_bytes"]) in lines[0]

    def test_the_ring_is_bounded(self, tmp_path, wired):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(50)
        r = _drain(pl, n, 20_000)
        for i in range(HD.UNFETCHED_RING + 10):
            HD.write_heartbeat(pl.root, [r], None, now=float(i))
        hb = json.load(open(HD.heartbeat_path(pl.root)))
        assert len(hb["sensors"]["nyquist"]["unfetched_recent"]) == HD.UNFETCHED_RING

    def test_a_loss_older_than_the_window_stops_failing_the_gate(self, tmp_path):
        root = str(tmp_path)
        os.makedirs(root, exist_ok=True)
        json.dump({"sensors": {"nyquist": {
            "kind": "node", "last_success_s": 100_000.0, "last_unfetched_bytes": 0,
            "unfetched_recent": [{"at": 100.0, "bytes": 735_000, "reason": None},
                                 {"at": 99_000.0, "bytes": 0, "reason": None}]}}},
            open(HD.heartbeat_path(root), "w"))
        assert HD.check(root, max_stale_s=7200.0, now=100_010.0,
                        unfetched_window_s=7200.0)[0] == 0
        assert HD.check(root, max_stale_s=7200.0, now=100_010.0,
                        unfetched_window_s=200_000.0)[0] == 1


class TestWatermarkFile:
    def test_it_lands_in_the_state_directory_the_pool_already_makes(self, tmp_path, wired):
        pl = P.Pool(str(tmp_path / "pool"))
        n = wired()
        n.grow(20)
        _drain(pl, n, 20_000)
        p = HD.watermark_path(pl.root)
        assert p == os.path.join(pl.state_dir, HD.SCENE_STATE_FILE)
        assert os.path.exists(p)
        wm = json.load(open(p))
        assert wm["nyquist"]["scene.csv"]["size"] == len(n.data)
        assert wm["nyquist"]["scene.csv"]["mean_row_bytes"] > 0

    def test_an_unreadable_watermark_reads_as_first_run_not_as_a_gap(self, tmp_path):
        root = str(tmp_path)
        os.makedirs(os.path.join(root, "state"), exist_ok=True)
        open(HD.watermark_path(root), "w").write("{ this is not json")
        assert HD.read_watermarks(root) == {}

    def test_two_nodes_keep_separate_marks(self, tmp_path, monkeypatch):
        pl = P.Pool(str(tmp_path / "pool"))
        for name in ("nyquist", "mach"):
            n = FakeNode(node=name)
            n.grow(20 if name == "nyquist" else 30)
            monkeypatch.setattr(HD, "fetch_status", n.status)
            monkeypatch.setattr(HD, "fetch_sd", n.sd)
            monkeypatch.setattr(HD, "_ls_sizes", n.ls)
            _drain(pl, n, 20_000)
        wm = HD.read_watermarks(pl.root)
        assert set(wm) == {"nyquist", "mach"}
        assert wm["nyquist"]["scene.csv"]["size"] != wm["mach"]["scene.csv"]["size"]
