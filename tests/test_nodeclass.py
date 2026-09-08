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
    assert nc.require_arrival("xiao-s3-pps").name == "xiao-s3-pps"
    assert nc.require_arrival("puc-pps").name == "puc-pps"


def test_the_ntp_penalty_is_metres_not_millimetres():
    """1-5 ms of cross-device sync is 0.34-1.7 m at 343 m/s. If this ever reads as centimetres,
    someone has quietly changed t_sigma_s and the refusal message has stopped being true."""
    m = nc.get("puc-ntp").range_sigma_m()
    assert 0.3 < m < 2.0
    assert nc.get("xiao-s3-pps").range_sigma_m() < 0.05


# ---------------------------------------------------------------- bandwidth
def test_xiao_is_nyquist_limited_not_microphone_limited():
    lo, hi, limit = nc.get("xiao-s3-pps").usable_band_hz()
    assert hi == 8000.0
    assert limit == "nyquist"


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
    assert "nyquist-limited" in msg
    assert "sample faster" in msg

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
    """c is not a constant; the node measures it. A cold night makes every class's range error
    smaller in metres for the same timing error, and the model should follow rather than pin 343."""
    warm = nc.get("puc-ntp").range_sigma_m(349.0)
    cold = nc.get("puc-ntp").range_sigma_m(331.3)
    assert cold < warm


# ---------------------------------------------------------------- construction guards
@pytest.mark.parametrize("kw", [
    {"time_source": "gps"},                 # not one of the three
    {"mic_count": 0},
    {"fs_hz": 0.0},
    {"t_sigma_s": 0.0},
    {"band_hz": (8000.0, 50.0)},            # inverted
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
    """The phone is refused for arrivals, and the reason is NOT its clock.

    Pinned because the obvious assumption -- "a phone has no PPS, so it is an NTP-class node" --
    is wrong in a way that would let it in. GPSTimingSync really does anchor (UTC, CLOCK_BOOTTIME)
    per GPS fix at a claimed 1-5 ms, which is puc-ntp territory. The audio path just never asks it.
    """

    def test_it_is_refused_for_arrivals(self):
        with pytest.raises(nc.CapabilityError):
            nc.require_arrival("gotchi-phone")

    def test_its_timing_is_worse_than_the_ntp_class_not_better(self):
        """If the phone were limited by GPSTimingSync it would beat puc-ntp's 3 ms. It is limited
        by the audio path instead, which is an order of magnitude worse."""
        phone = nc.get("gotchi-phone")
        ntp = nc.get("puc-ntp")
        assert phone.t_sigma_s > ntp.t_sigma_s
        assert phone.t_sigma_s / ntp.t_sigma_s > 5.0

    def test_the_error_exceeds_the_whole_surveyed_baseline(self):
        """nyquist->mach is 16.6 m. A node whose 1-sigma range error is half that cannot be
        averaged in with nodes at 3 cm -- it sets the answer on its own."""
        phone = nc.get("gotchi-phone")
        assert phone.t_sigma_s * 343.0 > 8.0

    def test_it_is_still_a_good_listener(self):
        """The refusal is about arrivals only. Refusing the whole node would throw away a 44.1 kHz
        microphone with a wider band than either XIAO node can reach."""
        phone = nc.get("gotchi-phone")
        assert phone.usable_band_hz()[1] > nc.get("xiao-s3-pps").usable_band_hz()[1]
        assert phone.fs_hz > nc.get("xiao-s3-pps").fs_hz
