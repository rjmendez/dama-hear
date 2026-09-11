"""A node's stamp must carry what it is worth, and the number must be worth having.

⚠️THE DEFECT THIS PINS. `time_valid` is set at exactly one site in hear_node.ino -- the first
NAV-PVT that names a PPS edge -- and NOTHING ever clears it. `local_to_utc()` then keeps
converting from the frozen `(edge_local_us, edge_unix_us)` pair for as long as the node runs. So
when the GPS UART dies mid-boot, which mach demonstrably does (PR #37), the node does NOT emit
`utc_us = 0`. It emits confident, silently wrong stamps that free-run on the ESP crystal, and
every counter it exports -- `fix`, `sats`, `pps`, `spread_us`, `time.valid` -- keeps reading fine.

The fix is not a refusal. A stamp still goes out; `sync_sigma_ns` goes out beside it.

WHAT IS ASSERTED HERE, and why each has teeth:

  * The firmware's drift bound covers the MEASURED distribution, not a sample of it. Lowering
    `STAMP_DRIFT_PPM_MAX` to any value the fleet has actually produced fails this file.
  * The base term is the anchor's own error and NOT the capture path. Charging the I2S block
    quantisation here as well would give one column two meanings across two producers.
  * The arithmetic the two halves must agree on: what anchor age the firmware's declared sigma
    spends the class budget at. Both sides are parsed from their own source, so this fails if
    either moves alone -- the same way tests/test_firmware_timebase.py ties
    FS_TIMEBASE_MIN_WIN_S to the class constant it was derived from.
  * The gate admits a healthy node at its MEASURED anchor age and refuses an hour-old anchor.

⚠️THE .ino IS PARSED, NOT GREPPED. Comments are stripped first and the constants are read out of
their `#define` lines, so this file's own prose cannot satisfy it.
"""
import binascii
import math
import pathlib
import re

import numpy as np
import pytest

from hear import corpus as C
from hear import detsfile as DF
from hear import nodeclass as NC
from hear import pool as POOL
from hear import sketch as SK
from hear.backend import associate as AS

INO = pathlib.Path(__file__).resolve().parents[1] / "firmware" / "hear_node" / "hear_node.ino"

# ---------------------------------------------------------------------------------------------
# THE MEASURED DRIFT DISTRIBUTION, 2026-09-10/11. `health.csv` and `health-prev.csv` were pulled
# from all three nodes (nyquist 172.16.100.105, rankine 172.16.100.50, mach 172.16.100.116) and
# the cumulative `esp_ppm` column differentiated: it is a running mean over `pps` intervals, so
# sum = n*(1e6 + ppm) and the rate over a window is
#     (n1*(1e6+p1) - n0*(1e6+p0)) / (n1 - n0) - 1e6
# Windows containing a `pps_gaps` change were discarded. Per window length, over all three nodes:
#     >= 900 s   n=148    4.359 .. 10.566 ppm
#     >= 300 s   n=461    4.194 .. 11.671 ppm
#     >= 120 s   n=1149  -4.770 .. 12.450 ppm   (the one negative is jitter, not a rate)
# Every other window is POSITIVE, so an unrefreshed anchor stamps LATE.
MEASURED_MAX_PPM = 12.450
#: The rate is a clean function of the node's own board temperature over the range the archive
#: covers (21.61 - 44.65 C): ppm = 15.920 - 0.2690 * T_C, n=148, residual sd 0.585 ppm.
#: ⚠️Nothing has been measured below 21.61 C, so the cold end is EXTRAPOLATION and is labelled as
#: such: 15.92 ppm at 0 C, 17.67 at fit + 3 sd.
MEASURED_FIT_AT_0C_PLUS_3SD_PPM = 17.67

#: Anchor age on the three live nodes, `time.since_edge_us` from GET /status, 2026-09-10 21:02
#: UTC: 567,359 / 607,331 / 664,057 us. This is what a HEALTHY node's stamp is built from, and
#: the gate must not refuse it.
LIVE_ANCHOR_AGE_US = (567359, 607331, 664057)


def _src():
    if not INO.exists():
        pytest.skip("hear_node.ino not in this checkout")
    t = INO.read_text()
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    return re.sub(r"//[^\n]*", "", t)


def _define(name):
    m = re.search(r"^#define\s+%s\s+([0-9.]+)u?\s*$" % re.escape(name), _src(), re.M)
    assert m, "%s is not defined in hear_node.ino" % name
    return float(m.group(1))


def _fw_sigma_ns(age_us):
    """The firmware's `stamp_sigma_ns`, re-implemented from the constants it declares."""
    return _define("STAMP_ANCHOR_SIGMA_US") * 1000.0 + age_us * _define("STAMP_DRIFT_PPM_MAX") / 1e3


class TestTheDriftBoundIsAnEnvelope:
    def test_it_covers_every_rate_this_fleet_has_produced(self):
        """⚠️A MEDIAN OR A SINGLE SAMPLE WOULD PASS EVERY OTHER TEST IN THIS FILE AND BE WRONG
        HALF THE TIME. The bound is the point of the exercise: under-declaring the uncertainty of
        a stamp is worse than not declaring it, because a number invites trust."""
        assert _define("STAMP_DRIFT_PPM_MAX") >= MEASURED_MAX_PPM

    def test_it_covers_the_cold_end_nothing_has_measured(self):
        """The rate is temperature-dependent and these are outdoor nodes. The coldest row in the
        archive is 21.61 C; the fit extrapolated to freezing is larger than anything observed,
        and the bound has to survive that or it is only a summer bound."""
        assert _define("STAMP_DRIFT_PPM_MAX") >= MEASURED_FIT_AT_0C_PLUS_3SD_PPM

    def test_it_is_not_so_loose_that_a_healthy_node_is_refused(self):
        """A bound can fail by being too big as well as too small: a node whose GPS is talking
        must still produce arrivals."""
        cls = NC.get("xiao-s3-pps")
        for age in LIVE_ANCHOR_AGE_US:
            assert cls.stamp_admissible(_fw_sigma_ns(age)) is True, age


class TestTheBaseTermIsTheAnchorAndNotTheCapturePath:
    def test_it_is_half_the_worst_measured_pps_spread(self):
        """MEASURED over 4558 health.csv rows from all three nodes 2026-09-10: interval-spread
        maxima 17 us (nyquist), 46 us (rankine), 33 us at p99 (mach). docs/timing.md already uses
        HALF the spread as the 1-sigma proxy for the PPS latch, so the fleet envelope is 23 us."""
        assert 23.0 <= _define("STAMP_ANCHOR_SIGMA_US") <= 30.0

    def test_it_excludes_the_i2s_block_quantisation(self):
        """⚠️THE TRANSPLANT THIS REPO KEEPS MAKING. 62.47 us of block quantisation is the biggest
        term in the detection-path budget and it belongs to the CAPTURE path -- it is already
        inside nodeclass's `t_sigma_s` and charged again as `path_bias_s`. `sync_sigma_ns` means
        the CLOCK anchor on the phone side, so a node that folded the capture path into it would
        publish one column with two meanings."""
        assert _define("STAMP_ANCHOR_SIGMA_US") < 62.47


class TestTheFirmwareAndTheBudgetAgreeOnWhatAStampCosts:
    def _max_age_s(self):
        head = NC.get("xiao-s3-pps").max_stated_clock_sigma_s() * 1e6 \
            - _define("STAMP_ANCHOR_SIGMA_US")
        return head / _define("STAMP_DRIFT_PPM_MAX")

    def test_a_healthy_anchor_age_fits_with_room(self):
        """The tolerance has to be comfortably above the age a working node actually stamps at,
        or every arrival is refused for the wrong reason."""
        assert self._max_age_s() > 2.0 * (max(LIVE_ANCHOR_AGE_US) / 1e6)

    def test_a_dead_uart_is_refused_in_seconds_not_hours(self):
        """mach ran 1016 s and then 1317 s with the timepulse advancing and NOT ONE NAV-PVT
        decoded (2026-09-10 health archive). Every stamp in those windows was admitted. The whole
        point is that they no longer are -- and not at the end of the window, near the start."""
        assert self._max_age_s() < 30.0

    def test_an_hour_of_free_run_is_over_the_budget_by_orders(self):
        """The field figure this closes: ~30 ms of accumulated error per hour, invisible in every
        counter the node exports. The declared bound is deliberately larger than the observed
        rate; either way an hour is not an arrival."""
        cls = NC.get("xiao-s3-pps")
        sig = _fw_sigma_ns(3600e6)
        assert cls.stamp_admissible(sig) is False
        assert sig / 1e9 > 100.0 * NC.ARRIVAL_T_SIGMA_MAX_S

    def test_the_drift_TERM_is_in_the_function_and_not_only_in_the_constants(self):
        """⚠️THE MUTATION THAT SURVIVED THE FIRST DRAFT OF THIS FILE. Every other test here
        re-implements `stamp_sigma_ns` from the #defines it declares, so deleting the drift term
        from the function body left all of them green while restoring the exact defect: a sigma
        that does not grow is `time_valid` wearing a number. The BODY is parsed -- braces
        matched, comments already stripped -- and the drift assignment has to use both the anchor
        age and the bound.
        """
        src = _src()
        i = src.index("static uint64_t stamp_sigma_ns(")
        j = src.index("{", i)
        depth, k = 0, j
        while k < len(src):
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        body = src[j:k + 1]
        assert "edge_local_us" in body, (
            "the age must be measured from the anchor the stamp was made against")
        drift = [ln for ln in body.splitlines() if "STAMP_DRIFT_PPM_MAX" in ln]
        assert len(drift) == 1, drift
        assert "age_us" in drift[0], (
            "the drift term must scale with the anchor's AGE; a constant is the defect again")
        assert "STAMP_ANCHOR_SIGMA_US" in body
        # and the age must be a magnitude, so a back-dated capture before the anchor edge does
        # not come out as a negative -- i.e. a smaller -- uncertainty
        assert body.count("age_us") >= 2

    def test_the_firmware_samples_the_sigma_where_it_samples_the_stamp(self):
        """A row waits up to CLIP_WAIT_MAX_S for its clip and sketch, and the anchor can be
        refreshed in that window. A sigma read at flush time would describe a different instant
        than the stamp beside it -- and it would describe a HEALTHIER one, which is the wrong
        direction to be wrong in."""
        src = _src()
        i = src.index("stamp_sigma_ns(cap_us)")
        blk = src[max(0, i - 400):i]
        assert "local_to_utc(cap_us" in blk, (
            "the sigma must be taken in audio_pump beside local_to_utc(cap_us), not at flush")


class TestTheClassBudgetAppliedPerDetection:
    def test_not_stated_is_neither_admitted_nor_refused(self):
        """Every dets.csv row before generation G6 has no sigma. A new field that read as a
        refusal would retroactively delete the whole node corpus."""
        assert NC.get("xiao-s3-pps").stamp_admissible(None) is None

    def test_a_stated_sigma_is_combined_with_the_class_and_not_compared_alone(self):
        """⚠️THE OPTIMISTIC READING THIS REJECTS. `sync_sigma_ns <= ARRIVAL_T_SIGMA_MAX_S` would
        let a node spend the WHOLE 129.4 us per-node budget on its clock while the class already
        declares 100 us of capture error. The two combine in RSS, so the clock may spend
        sqrt(129.4**2 - 100**2) = 82.1 us and no more."""
        cls = NC.get("xiao-s3-pps")
        head_ns = cls.max_stated_clock_sigma_s() * 1e9
        assert head_ns < NC.ARRIVAL_T_SIGMA_MAX_S * 1e9
        assert cls.stamp_admissible(head_ns * 0.99) is True
        assert cls.stamp_admissible(head_ns * 1.01) is False
        assert math.isclose(cls.stamp_t_sigma_s(head_ns), NC.ARRIVAL_T_SIGMA_MAX_S, rel_tol=1e-9)

    def test_a_class_already_over_the_bound_cannot_be_rescued_by_a_good_statement(self):
        """gotchi-phone declares 5 ms on its clock alone. A payload claiming 1 us of sync does
        not make it an arrival source; the class figure is the floor."""
        cls = NC.get("gotchi-phone")
        assert cls.max_stated_clock_sigma_s() == 0.0
        assert cls.stamp_admissible(1000.0) is False

    def test_the_gate_is_live_on_a_survey_that_names_no_class(self):
        """⚠️THE SEAM THIS WOULD HAVE SHIPPED WITH. The shipped survey.json declares `class` on
        NO node, and `Survey.arrival_ids()` admits an unstated class by design -- so a gate that
        required a declared class would have been dead code on the only array that exists.
        Unstated charges the STRICTEST arrival class instead."""
        import json
        import pathlib as _p
        sv = json.loads((_p.Path(__file__).resolve().parents[1] / "survey.json").read_text())
        unclassed = sorted(n["name"] for n in sv["nodes"] if not n.get("class"))
        assert unclassed == ["mach", "nyquist", "rankine"], (
            "the three XIAO nodes name no class in survey.json, which is what makes the unstated "
            "fallback the live path; if that changed, check the stated class is what is charged")
        head = NC.strictest_arrival_class().max_stated_clock_sigma_s() * 1e9
        assert NC.stamp_admissible(head * 1.01, None) is False
        assert NC.stamp_admissible(head * 0.99, None) is True
        assert NC.stamp_admissible(None, None) is None

    def test_an_unrecognised_class_is_charged_the_strictest_and_says_so(self):
        """A producer this version does not understand is not one to take a budget from."""
        head = NC.strictest_arrival_class().max_stated_clock_sigma_s() * 1e9
        why = NC.stamp_refusal(head * 2, "some-future-node")
        assert why and "unstated" in why

    def test_adding_a_class_can_only_tighten_the_fallback(self, monkeypatch):
        """⚠️ASSERTED AGAINST A SECOND CLASS, BECAUSE THE REGISTRY HAS ONLY ONE. Today exactly
        one class is an arrival source, so `min` and `max` over that set are the same value and
        a test written against the live registry cannot tell them apart -- it passed with the
        fallback reversed. A looser admissible class is registered temporarily; the fallback must
        ignore it."""
        adm = [c for c in NC.CLASSES.values() if c.contributes_arrival()]
        assert adm, "no admissible class at all would make the fallback undefined"
        tight = NC.strictest_arrival_class().max_stated_clock_sigma_s()
        looser = NC.NodeClass(
            name="test-looser", time_source="gps_pps",
            t_sigma_s=NC.ARRIVAL_T_SIGMA_MAX_S / 10.0,   # far more headroom than xiao-s3-pps
            path_bias_s=1e-6, mic_count=1, fs_hz=16000.0, band_hz=(50.0, 8000.0))
        assert looser.contributes_arrival()
        assert looser.max_stated_clock_sigma_s() > tight
        monkeypatch.setitem(NC.CLASSES, "test-looser", looser)
        assert NC.strictest_arrival_class().max_stated_clock_sigma_s() == tight, (
            "a new class widened the budget charged to a receiver whose class is unstated")

    def test_the_refusal_carries_numbers(self):
        why = NC.get("xiao-s3-pps").stamp_refusal(500000.0)
        assert why and "us" in why and "m of range" in why
        assert NC.get("xiao-s3-pps").stamp_refusal(1000.0) is None


def _frame_hex():
    rng = np.random.default_rng(7)
    q, ref = SK.sketch(rng.normal(0, 1000, 4096), 16000.0)
    return binascii.hexlify(SK.pack(597174, ref, 1140, q, fs=16000.0)).decode()


class TestTheUncertaintyReachesTheWire:
    HDR = ",".join(DF.G6.declared)

    def _row(self, sigma="41000"):
        vals = ["nyquist", "1789088548567359", "19625", "12345", "18485", "5000", "900", "4096",
                "16000.000", "736", _frame_hex(), "", "quiet", sigma]
        return self.HDR + "\n" + ",".join(vals) + "\n"

    def test_g6_is_recognised_and_g5_is_still_readable(self):
        assert DF.identify(DF.G6.declared) is DF.G6
        assert DF.identify(DF.G5.declared) is DF.G5
        assert DF.LATEST is DF.G6

    def test_a_stated_sigma_survives_the_reader(self):
        got = DF.read_text(self._row())
        assert got.generation is DF.G6
        assert got.rows and got.rows[0]["sync_sigma_ns"] == 41000.0

    def test_an_empty_cell_is_not_stated_and_is_not_zero(self):
        """⚠️0 WOULD READ AS A PERFECT CLOCK. A row the node could not stamp has no anchor to be
        uncertain about, so the column is empty and the reader says None."""
        got = DF.read_text(self._row(sigma=""))
        assert got.rows[0]["sync_sigma_ns"] is None
        assert POOL._record_from_node_row(got.rows[0])["sync_sigma_ns"] is None
        # and a literal 0, which the firmware never writes, must not survive the pool either
        zero = DF.read_text(self._row(sigma="0"))
        assert POOL._record_from_node_row(zero.rows[0])["sync_sigma_ns"] is None

    @pytest.mark.parametrize("cell", ["0", "0.0", "-1", "  ", "nonsense"])
    def test_the_READER_refuses_a_non_positive_cell_and_not_only_the_pool(self, cell):
        """⚠️THE LAYER THE ASSERTION IS MADE AT IS THE POINT. Every zero-sigma test above went
        through `POOL._record_from_node_row`, which has its own non-positive guard -- so the
        reader could (and did) leave the string "0" in the row while the suite stayed green. A
        public field documented as "not stated or a number" was handing out "0", and
        `nodeclass.stamp_admissible("0")` returns True: a perfect clock.

        The rule has one implementation now, `detsfile.stated_sigma_ns`; the pool calls it.
        """
        got = DF.read_text(self._row(sigma=cell))
        assert got.rows[0]["sync_sigma_ns"] is None, (
            "the reader left %r in a field its own docstring says can only be a positive number "
            "or None" % (got.rows[0]["sync_sigma_ns"],))
        assert POOL._sync_sigma_ns(cell) is DF.stated_sigma_ns(cell), "one rule, one copy"

    def test_a_g5_width_row_under_a_g6_header_is_a_counted_refusal(self):
        """The whole reason hear/detsfile.py exists: a writer that disagrees with its own header
        must be refused by width, never read one column short."""
        short = self.HDR + "\n" + ",".join(
            ["nyquist", "1", "2", "3", "4", "5", "6", "7", "8.0", "736", _frame_hex(), "",
             "quiet"])
        got = DF.read_text(short + "\n")
        assert not got.rows
        assert sum(got.counts.values()) == 1
        assert "row_width_13_expected_14" in got.counts

    def test_the_pool_record_carries_it_under_the_phone_s_name_and_unit(self):
        """⚠️SAME KEY, SAME UNIT. hear/pool.py reads `sync_sigma_ns` off an MQTT payload for a
        phone; a node stating the same quantity under a second name would be a defect, not a
        convenience."""
        row = DF.read_text(self._row()).rows[0]
        rec = POOL._record_from_node_row(row)
        assert rec["sync_sigma_ns"] == 41000.0
        assert "sync_sigma_ns" in POOL._RECORD_FIELDS

    def test_a_node_row_does_not_become_clock_trusted_by_stating_a_sigma(self):
        """`utc_trusted` is a PHONE scale (clock_tier). A node's clock trust is a different
        measurement and must stay None however good its sigma is."""
        row = DF.read_text(self._row()).rows[0]
        rec = POOL._record_from_node_row(row)
        assert C.utc_trusted_of(rec) is None


class TestRefusedAsAnArrivalAndKeptForEverythingElse:
    def test_associate_refuses_a_stated_false(self):
        assert AS.arrival_is_usable({"stamp_admissible": False}) is False
        assert "stamp_admissible" in AS._QUALITY_FLAGS

    def test_absent_and_none_stay_usable(self):
        """Absent means usable, by design: the gate must be a strict no-op on every detection
        recorded before the producers started emitting the field."""
        assert AS.arrival_is_usable({}) is True
        assert AS.arrival_is_usable({"stamp_admissible": None}) is True
        assert AS.arrival_is_usable({"stamp_admissible": True}) is True

    def test_the_refusal_names_the_stale_anchor_and_not_the_detection(self):
        why = AS._unusable_reason({"node_id": 1, "t_utc_s": 1.0, "stamp_admissible": False})
        assert "anchor" in why and "crystal" in why

    def test_the_row_is_still_ingested(self):
        """⚠️LOSING THE ROW IS THE WRONG OUTCOME. A detection whose arrival time is inadmissible
        is still a real acoustic event: it trains a classifier and it belongs in the scene. The
        pool stores it, and `records()` returns it, exactly as before."""
        hdr = ",".join(DF.G6.declared)
        over = str(int(NC.ARRIVAL_T_SIGMA_MAX_S * 1e9 * 10))
        vals = ["nyquist", "1789088548567359", "19625", "12345", "18485", "5000", "900", "4096",
                "16000.000", "736", _frame_hex(), "", "quiet", over]
        got = DF.read_text(hdr + "\n" + ",".join(vals) + "\n")
        assert len(got.rows) == 1
        rec = POOL._record_from_node_row(got.rows[0])
        assert rec["anchored"] is True
        assert NC.get("xiao-s3-pps").stamp_admissible(rec["sync_sigma_ns"]) is False
