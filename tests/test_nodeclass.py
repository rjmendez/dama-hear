#!/usr/bin/env python3
"""The capability model refuses rather than degrades.

The tests that matter here are the REFUSALS. A capability model that only proves the happy path
is a lookup table; the point of this one is that a node which cannot do something is stopped at
the door instead of contributing a plausible-looking wrong number.
"""
import math

import pytest

from hear import nodeclass as nc


# ---------------------------------------------------------------- registry sanity
def test_the_two_built_nodes_are_the_same_class():
    """nyquist and mach are both xiao-s3-pps. Their PARTS differ -- mach carries a real BME280 and
    a QMC5883L, nyquist a BMP280 and an IST8310 -- but nothing in the class model depends on which
    magnetometer is fitted, and both have temperature, which is the sensor that changes an answer."""
    c = nc.get("xiao-s3-pps")
    assert c.contributes_arrival()
    assert "temp" in c.env


def test_every_registered_class_has_a_positive_timing_error():
    """A class claiming zero timestamp error would be claiming hardware nobody has."""
    for name, c in nc.CLASSES.items():
        assert c.t_sigma_s > 0, name
        assert c.range_sigma_m() > 0, name


def test_unknown_class_raises_and_names_what_exists():
    with pytest.raises(nc.CapabilityError) as e:
        nc.get("t1-webcam")
    assert "xiao-s3-pps" in str(e.value)


# ---------------------------------------------------------------- the door
def test_ntp_node_is_refused_as_an_arrival_source():
    """The whole reason the model exists. puc-ntp is a better listener than either XIAO node and
    is still not admissible as a TDoA arrival."""
    with pytest.raises(nc.CapabilityError) as e:
        nc.require_arrival("puc-ntp", node_id=4)
    msg = str(e.value)
    assert "node 4" in msg
    assert "ntp" in msg
    assert "m of range" in msg          # the refusal states the cost, not just the rule


def test_pps_node_is_admitted():
    """xiao-s3-pps is the ONE class in the registry that passes both predicates: a measured 100 us
    clock, and a capture path the firmware back-dates to within one sample."""
    assert nc.require_arrival("xiao-s3-pps").name == "xiao-s3-pps"
    assert [n for n, c in nc.CLASSES.items() if c.contributes_arrival()] == ["xiao-s3-pps"]


def test_a_pps_clock_is_not_enough_on_its_own():
    """⚠️BEHAVIOUR CHANGE, AND IT IS THE POINT. puc-pps used to be admitted, because the gate read
    `time_source == "gps_pps"` and that string was all it took. Nothing about the hardware has
    changed: the PUC is closed firmware whose audio path nobody has instrumented, so the delay
    between its diaphragm and its timestamp is unknown, and an unknown constant offset is exactly
    the error a 3-node exactly-determined fit cannot report."""
    puc = nc.get("puc-pps")
    assert puc.clock_admissible(), "its CLOCK is not what stops it"
    assert not puc.capture_bias_bounded()
    assert not puc.contributes_arrival()
    with pytest.raises(nc.CapabilityError) as e:
        nc.require_arrival("puc-pps", node_id=4)
    msg = str(e.value)
    assert "NEVER BEEN MEASURED" in msg
    assert "t_sigma" not in msg, "it must not be blamed for a clock that passes"


def test_the_ntp_penalty_is_metres_not_millimetres():
    """1-5 ms of cross-device sync is 0.34-1.7 m at 343 m/s. If this ever reads as centimetres,
    someone has quietly changed t_sigma_s and the refusal message has stopped being true."""
    m = nc.get("puc-ntp").range_sigma_m()
    assert 0.3 < m < 2.0
    assert nc.get("xiao-s3-pps").range_sigma_m() < 0.05


# ---------------------------------------------------------------- bandwidth
def test_xiao_is_microphone_limited_not_nyquist_limited():
    lo, hi, limit = nc.get("xiao-s3-pps").usable_band_hz()
    assert hi == 10000.0
    assert limit == "microphone"


def test_the_i2s_variant_is_microphone_limited_and_says_so():
    """docs/node-hardware.md: the ICS-43434 has no ultrasonic content AT ANY SAMPLE RATE. At 48 kHz
    the part binds before Nyquist does, and the remedy differs, so the model must distinguish."""
    lo, hi, limit = nc.get("xiao-s3-i2s").usable_band_hz()
    assert hi == 15000.0
    assert limit == "microphone"


def test_cicada_band_is_reachable_and_ultrasonic_katydids_are_not():
    """The measured constraint from modules/bioacoustic: 4-8 kHz is inside what a 16 kHz node
    delivers; 15-40 kHz is outside every class in the registry."""
    assert nc.get("xiao-s3-pps").can_hear(4000, 7900)
    for name in nc.CLASSES:
        assert not nc.get(name).can_hear(15000, 40000), name


def test_band_refusal_names_the_ceiling_and_the_remedy():
    with pytest.raises(nc.CapabilityError) as e:
        nc.require_band("xiao-s3-pps", 4000, 12000, node_id="nyquist")
    msg = str(e.value)
    assert "microphone-limited" in msg
    assert "no sample rate fixes this" in msg

    # No registered class is Nyquist-bound now, so the other label is exercised on a built one.
    slow = nc.NodeClass(name="t-8k", time_source="gps_pps", t_sigma_s=1e-4, mic_count=1,
                        fs_hz=8000.0, band_hz=(50.0, 10000.0), env=(), raw_retain_s=0.0, notes="")
    assert slow.usable_band_hz() == (50.0, 4000.0, "nyquist")

    with pytest.raises(nc.CapabilityError) as e:
        nc.require_band("xiao-s3-i2s", 4000, 20000)
    msg = str(e.value)
    assert "microphone-limited" in msg
    assert "no sample rate fixes this" in msg


# ---------------------------------------------------------------- bearing
def test_only_two_mic_classes_can_bear():
    assert not nc.get("xiao-s3-pps").can_bear()
    assert nc.get("puc-ntp").can_bear()
    assert nc.get("puc-pps").can_bear()


# ---------------------------------------------------------------- mixed networks
def test_timing_budget_exposes_the_weakest_link_before_the_solve():
    b = nc.timing_budget_m(["xiao-s3-pps", "puc-ntp", "xiao-s3-pps"])
    assert set(b) == {"xiao-s3-pps", "puc-ntp"}          # deduplicated, order preserved
    assert b["puc-ntp"] > 20 * b["xiao-s3-pps"]


def test_sound_speed_moves_the_budget():
    """c is not a constant; the node measures it. A cold spell makes every class's range error
    smaller in metres for the same timing error, and the model should follow rather than pin 343."""
    warm = nc.get("puc-ntp").range_sigma_m(349.0)
    cold = nc.get("puc-ntp").range_sigma_m(331.3)
    assert cold < warm


# ---------------------------------------------------------------- the gate is a MEASURED test
def _candidate(**kw):
    """A receiver that is not in the registry, so these tests describe the GATE and not a class.

    The defaults are a plausible fourth receiver: a single-board host disciplined by chrony rather
    than by a PPS pin, sampling six mics at 48 kHz. The clock figure is the kind of number such a
    host measures; whether any particular machine achieves it is not what is under test here.
    """
    base = dict(name="candidate", time_source="ntp", t_sigma_s=124e-6, mic_count=6,
                fs_hz=48000.0, band_hz=(50.0, 15000.0), path_bias_s=20e-6)
    base.update(kw)
    return nc.NodeClass(**base)


def test_the_thresholds_are_derived_from_one_sourced_number():
    """docs/node-hardware.md: one degree of unmeasured air temperature is 0.606 m/s, which over
    35 m is 35 * 0.606 / 343**2 = 183 us. Everything the gate enforces is arithmetic on that, so
    if the budget ever moves both bounds move with it rather than drifting apart."""
    assert nc.ARRIVAL_ONE_WAY_BUDGET_S == pytest.approx(35 * 0.606 / 343.0 ** 2, rel=0.02)
    assert nc.ARRIVAL_T_SIGMA_MAX_S == pytest.approx(nc.ARRIVAL_ONE_WAY_BUDGET_S / math.sqrt(2))
    assert nc.ARRIVAL_PATH_BIAS_MAX_S == pytest.approx(nc.ARRIVAL_ONE_WAY_BUDGET_S / 2.0)


def test_the_bias_bound_is_tighter_than_the_sigma_bound():
    """Not an accident and not a preference. A TDoA is a difference of two arrivals: two
    INDEPENDENT clock errors combine in RSS (sqrt(2)), two DETERMINISTIC offsets combine linearly
    in the worst case (2). Same budget, different divisor, and the bias ends up held tighter --
    which is also the right direction, because scatter averages down over events and bias does
    not."""
    assert nc.ARRIVAL_PATH_BIAS_MAX_S < nc.ARRIVAL_T_SIGMA_MAX_S
    assert nc.ARRIVAL_T_SIGMA_MAX_S / nc.ARRIVAL_PATH_BIAS_MAX_S == pytest.approx(2 / math.sqrt(2))


def test_a_good_clock_is_admitted_whatever_its_source_is_called():
    """⚠️THE WHOLE POINT OF THE CHANGE. The gate used to read `time_source == "gps_pps"`, so a
    receiver disciplined by anything else was refused for the NAME of its clock rather than the
    size of its error. A host whose clock MEASURES 124 us is inside the 129.4 us bound and is
    admissible on the clock, and the string "ntp" no longer has a vote."""
    c = _candidate()
    assert c.time_source != "gps_pps"
    assert c.clock_admissible()
    assert c.contributes_arrival()


def test_a_bad_clock_is_refused_however_it_labels_itself():
    """The converse, and the reason a string test was never safe in either direction: calling
    yourself gps_pps buys nothing if what you measure is milliseconds."""
    c = _candidate(time_source="gps_pps", t_sigma_s=3e-3)
    assert not c.clock_admissible()
    assert not c.contributes_arrival()


class TestTheBiasPredicateIsNotOptional:
    """⚠️THE LOOPHOLE THESE TESTS EXIST TO CLOSE.

    Turning the gate into a sigma test invites exactly one failure: admitting a receiver with a
    beautiful clock and an uncharacterised audio path. Its capture delay is a BIAS -- it does not
    average down over events, and in a 3-node solve two independent TDoAs exactly determine (x, y)
    with z fixed, so the residual is identically zero by construction and can never report it. A
    receiver like that produces confident wrong answers that nothing downstream can detect, which
    is strictly worse than one that is refused.
    """

    def test_an_excellent_clock_with_an_unmeasured_path_is_still_refused(self):
        c = _candidate(path_bias_s=None)
        assert c.clock_admissible(), "the clock is not the problem"
        assert not c.capture_bias_bounded()
        assert not c.contributes_arrival()

    def test_the_refusal_says_it_is_the_path_and_says_why_it_cannot_wait(self):
        c = _candidate(path_bias_s=None)
        why = c.arrival_refusals()
        assert len(why) == 1
        assert "NEVER BEEN MEASURED" in why[0]
        assert "does not average down" in why[0]
        assert "residual is identically zero" in why[0]

    def test_a_measured_but_large_bias_is_refused_with_its_cost_in_metres(self):
        """Measuring it is not the same as passing. A 1 ms path is 0.34 m of range on one
        receiver, which is 11x the bound."""
        c = _candidate(path_bias_s=1e-3)
        assert not c.capture_bias_bounded()
        why = c.arrival_refusals()
        assert len(why) == 1
        assert "0.343 m" in why[0]

    def test_unmeasured_is_not_zero_anywhere_it_could_be_summed(self):
        """None must stay None all the way out to the caller. A bias that silently reads as 0.0 in
        a budget table is the same mistake as admitting the receiver, one layer further on."""
        c = _candidate(path_bias_s=None)
        assert c.path_bias_m() is None
        assert nc.arrival_budget_m(["puc-pps"])["puc-pps"]["bias_m"] is None

    def test_a_class_may_not_claim_a_perfect_capture_path(self):
        """Zero uncorrected delay is a claim no hardware supports, and it would be the cheapest
        way to walk straight through this predicate."""
        with pytest.raises(nc.CapabilityError) as e:
            _candidate(path_bias_s=0.0)
        assert "never measured" in str(e.value)


def test_an_undisciplined_clock_is_refused_however_small_its_stated_sigma():
    """The one string comparison that survives, and it is a floor rather than a label check.
    `time_source "none"` says nothing steers this clock to UTC at all, so its stated t_sigma
    describes a free-running oscillator and there is nothing to compare against a threshold.
    Without this a class could declare "none" and 1 us and be admitted on the strength of a number
    that means nothing."""
    c = _candidate(time_source="none", t_sigma_s=1e-6)
    assert c.t_sigma_s < nc.ARRIVAL_T_SIGMA_MAX_S
    assert not c.clock_admissible()
    assert not c.contributes_arrival()
    assert "time_source" in c.arrival_refusals()[0]


@pytest.mark.parametrize("field,bound", [
    ("t_sigma_s", "ARRIVAL_T_SIGMA_MAX_S"),
    ("path_bias_s", "ARRIVAL_PATH_BIAS_MAX_S"),
])
def test_each_bound_is_inclusive_and_bites_immediately_past_it(field, bound):
    """A threshold nobody has probed at the edge is a threshold nobody knows the sense of."""
    limit = getattr(nc, bound)
    assert _candidate(**{field: limit}).contributes_arrival()
    assert not _candidate(**{field: limit * 1.001}).contributes_arrival()


def test_the_admitted_class_has_margin_rather_than_scraping_through():
    """xiao-s3-pps is the array. If a threshold change ever left it passing by 1%, that is a
    result worth seeing in a test failure rather than in a field session."""
    x = nc.get("xiao-s3-pps")
    assert x.t_sigma_s / nc.ARRIVAL_T_SIGMA_MAX_S < 0.85
    assert x.path_bias_s / nc.ARRIVAL_PATH_BIAS_MAX_S < 0.85
    assert x.path_bias_s == pytest.approx(1.0 / x.fs_hz), \
        "its bias IS one sample of its own rate -- the residual of the firmware's back-date"


def test_arrival_budget_reports_both_halves_and_the_verdict():
    b = nc.arrival_budget_m(["xiao-s3-pps", "gotchi-phone", "xiao-s3-pps"])
    assert set(b) == {"xiao-s3-pps", "gotchi-phone"}          # deduplicated, order preserved
    assert b["xiao-s3-pps"]["admissible"] is True
    assert b["gotchi-phone"]["admissible"] is False
    assert b["gotchi-phone"]["bias_m"] > b["gotchi-phone"]["sigma_m"]


# ---------------------------------------------------------------- construction guards
@pytest.mark.parametrize("kw", [
    {"time_source": "gps"},                 # not one of the three
    {"mic_count": 0},
    {"fs_hz": 0.0},
    {"t_sigma_s": 0.0},
    {"band_hz": (8000.0, 50.0)},            # inverted
    {"path_bias_s": 0.0},                   # "my capture path is perfect"
    {"path_bias_s": -1e-6},
    {"path_bias_s": float("nan")},
])
def test_bad_class_definitions_raise(kw):
    base = dict(name="bad", time_source="gps_pps", t_sigma_s=1e-4, mic_count=1,
                fs_hz=16000.0, band_hz=(50.0, 10000.0))
    base.update(kw)
    with pytest.raises(nc.CapabilityError):
        nc.NodeClass(**base)


def test_registering_a_duplicate_name_raises():
    with pytest.raises(nc.CapabilityError):
        nc.register(nc.get("xiao-s3-pps"))


def test_describe_covers_every_class_and_states_the_limit():
    t = nc.describe()
    for name in nc.CLASSES:
        assert name in t
    assert "nyq" in t and "mic" in t        # the truncated limit column


def test_puc_pps_is_marked_unverified():
    """It is in the registry so the model is complete, and its notes must keep saying that the PPS
    routing has never been confirmed. A provisional class that stops announcing itself as
    provisional is how a guess becomes a fact."""
    assert "UNVERIFIED" in nc.get("puc-pps").notes


class TestGotchiPhone:
    """The phone is refused for arrivals on TWO counts, and the audio path is the bigger one.

    Pinned because both obvious assumptions are wrong. "A phone has no PPS, so it is untimed" is
    wrong -- GPSTimingSync anchors (UTC, CLOCK_BOOTTIME) per GPS fix at a published location-tier
    5 ms, and the audio path now reads a HAL capture anchor rather than a delivery-thread clock.
    "Its clock is the problem, then" is also wrong -- 5 ms of scatter is bad, and 13.1 ms of
    UNCORRECTED capture latency on the best of three devices is worse and is a bias, so it neither
    averages down over events nor shows up in the residual of a 3-node fit.
    """

    def test_it_is_refused_for_arrivals(self):
        with pytest.raises(nc.CapabilityError):
            nc.require_arrival("gotchi-phone")

    def test_the_audio_path_is_the_bigger_problem_not_the_clock(self):
        """Both terms disqualify it, but not equally, and the model must keep them apart.

        Its clock is GPSTimingSync's own location-tier 5 ms -- worse than puc-ntp's 3 ms, but the
        same order. Its UNCORRECTED capture latency is 13.122 ms, which is 2.6x the clock term and
        4.50 m of range. Reporting one number would hide which one to go and fix.
        """
        phone = nc.get("gotchi-phone")
        ntp = nc.get("puc-ntp")
        assert phone.t_sigma_s > ntp.t_sigma_s
        assert phone.path_bias_s > phone.t_sigma_s
        assert not phone.clock_admissible()
        assert not phone.capture_bias_bounded()

    def test_the_bias_is_metres_and_it_does_not_average_down(self):
        """13.122 ms is the BEST of three devices (the other measured one is 293 ms, past the
        app's own 100 ms plausibility bound, and the third has no entry). Even the best case is
        4.50 m -- and unlike scatter, collecting more events does not shrink it by a millimetre."""
        phone = nc.get("gotchi-phone")
        assert phone.path_bias_m() == pytest.approx(4.50, abs=0.01)
        assert phone.path_bias_s / nc.ARRIVAL_PATH_BIAS_MAX_S > 100.0

    def test_the_refusal_names_both_reasons_not_just_the_first(self):
        """A receiver refused for its clock is usually also unmeasured on its path. Reporting one
        at a time turns one fix into two round trips through the field."""
        with pytest.raises(nc.CapabilityError) as e:
            nc.require_arrival("gotchi-phone", node_id="myasshurts")
        msg = str(e.value)
        assert "myasshurts" in msg
        assert "clock t_sigma" in msg
        assert "capture-path delay" in msg
        assert len(nc.get("gotchi-phone").arrival_refusals()) == 2

    def test_an_arrival_time_does_leave_the_phone(self):
        """⚠️THIS ENTRY'S PREVIOUS REVISION SAID "There is no arrival time leaving the phone today
        at all", and that was false. The phone HAL-anchors its frame axis, publishes an onset and
        a sync sigma, and retains a raw ring it will serve on request -- which is precisely why
        the refusal has to rest on the capture-path number and nothing else. Pinned here as a
        property of the class, so a future edit cannot quietly reinstate the old story.
        """
        phone = nc.get("gotchi-phone")
        assert phone.time_source != "none", "it is disciplined; it is just not disciplined WELL"
        assert phone.raw_retain_s > 0.0, "raw PCM is retained and pullable"
        assert phone.path_bias_s is not None, "its latency has been measured, badly"

    def test_it_is_still_a_good_listener(self):
        """The refusal is about arrivals only. Refusing the whole node would throw away a 44.1 kHz
        microphone with a wider band than either XIAO node can reach."""
        phone = nc.get("gotchi-phone")
        assert phone.usable_band_hz()[1] > nc.get("xiao-s3-pps").usable_band_hz()[1]
