"""The PUC's compiled 1PPS pin, pinned against the measurement that chose it.

⚠️THIS LINE HAS BEEN REVERTED TWICE, BY TWO DIFFERENT ROUTES, AND NEITHER WAS NOTICED.

  1. a6bbdab established that GPIO18 is already driven and wrote `PPS_PIN -1` into
     firmware/boards/puc.h -- a file `grep -rn "puc\\.h"` shows is included by NOTHING. The header
     held the correct value while puc_node.ino, the file that actually compiles, kept 18. The live
     node advertised 18 on /pins for two months.
  2. cfe3ec4 fixed it in puc_node.ino. b2d47de ("platform: one failback, and healthy that cannot
     forget to mean reachable") reverted it back to 18 as a side effect of a 125-line refactor
     carried on a branch holding an older copy of the file.

Both reverts were silent because nothing compared the compiled constant against the measurement.
This is that comparison. It reads the #define rather than grepping for a number, so it cannot pass
by matching its own prose.

THE MEASUREMENT (live board, /scanpu + /scanpd, 2026-09-10):

    gpio   pullup   pulldown   reading
      15    HIGH      low      floats -- free
      16    HIGH      low      floats -- free
      17    HIGH      low      free, and where the joint is
      18    low       low      HELD LOW -- something external owns this net
      39    low       low      HELD LOW
      38    8 edges, 50% duty -- the DS3231 1 Hz, proving the scan can see 1 Hz at all

A floating pin follows whichever internal resistor is engaged; 18 and 39 do not. The L86's 1PPS is
push-pull (Hardware Design Table 3, VOHmin 2.4 V), so landing it on a driven net is two drivers on
one signal.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INO = os.path.join(ROOT, "firmware", "puc_node", "puc_node.ino")
HDR = os.path.join(ROOT, "firmware", "boards", "puc.h")

#: Measured as externally driven. Not free, whatever a comment says.
DRIVEN_PINS = {8, 18, 39}
#: Measured as floating in both polarities.
FREE_PINS = {15, 16, 17}
#: The joint the operator actually made, traced at both ends (L86 pin 5 -> backup battery
#: independently confirms the module pin numbering).
WIRED_PIN = 17


def _define(path, name):
    """The value of a #define, from the file that compiles it. None if absent."""
    with open(path) as fh:
        src = fh.read()
    # last definition wins, the way the preprocessor sees it
    hits = re.findall(r"^\s*#define\s+%s\s+(-?\d+)" % re.escape(name), src, re.M)
    return int(hits[-1]) if hits else None


class TestTheCompiledPin:
    def test_puc_node_ino_defines_the_measured_pin(self):
        got = _define(INO, "PPS_PIN")
        assert got is not None, "puc_node.ino has no PPS_PIN at all"
        assert got == WIRED_PIN, (
            "puc_node.ino compiles PPS_PIN %d. It has been reverted to 18 twice; see this "
            "module's docstring. %d is measured as externally driven." % (got, got))

    def test_the_compiled_pin_is_not_one_measured_as_driven(self):
        # stated separately from the equality above: if the wire is ever moved, this is the
        # constraint that still has to hold, and the equality test would just be updated
        got = _define(INO, "PPS_PIN")
        assert got not in DRIVEN_PINS, (
            "PPS_PIN %d is externally driven (measured HELD LOW against an internal pullup). "
            "The L86's 1PPS is push-pull, so this is two drivers on one net." % got)

    def test_the_compiled_pin_is_one_measured_as_free(self):
        assert _define(INO, "PPS_PIN") in FREE_PINS

    @pytest.mark.parametrize("pin", sorted(DRIVEN_PINS))
    def test_the_driven_pins_are_named_so_a_future_choice_is_informed(self, pin):
        # the point is not that 18 is bad, it is that 8/18/39 are all bad and only 15/16/17 are
        # known good -- a future move must not rediscover this with an iron in hand
        assert pin not in FREE_PINS


class TestTheDeadHeaderDoesNotContradictIt:
    """⚠️puc.h is included by NOTHING and still reads authoritative.

    It is not deleted here -- that is a bigger call -- but it must not disagree with the file that
    compiles, because disagreeing is precisely how the first revert survived two months.
    """

    def test_the_header_does_not_assert_a_driven_pin(self):
        got = _define(HDR, "PPS_PIN")
        assert got is None or got == -1 or got == WIRED_PIN, (
            "puc.h says PPS_PIN %s. It compiles nowhere, so it can only mislead; -1 or the "
            "measured pin are the only honest values." % got)

    def test_the_header_points_at_the_file_that_builds(self):
        with open(HDR) as fh:
            src = fh.read()
        assert "puc_node.ino" in src, (
            "puc.h must name the file that actually compiles, or a reader takes its own PPS_PIN "
            "as authoritative -- which is how GPIO18 survived for two months")


def _define_list(path, name):
    """The int members of a brace-initialised #define, e.g. {15, 16, 17}. None if absent."""
    with open(path) as fh:
        src = fh.read()
    hits = re.findall(r"^\s*#define\s+%s\s+\{([^}]*)\}" % re.escape(name), src, re.M)
    if not hits:
        return None
    return [int(x) for x in re.findall(r"-?\d+", hits[-1])]


class TestTheDeadHeaderDoesNotOfferAPinTheScanDisqualified:
    """⚠️THE PPS_PIN GUARD ABOVE WAS BLIND TO THE LIST THREE LINES BELOW IT.

    puc.h's FREE_PADS read {15, 16, 17, 18, 21, 38, 39} while this module's own docstring
    recorded 18 and 39 as HELD LOW and 38 as carrying the DS3231's 1 Hz SQW. A guard aimed at one
    occurrence of a defect does not see the next one, so this parses the list too.

    21 is absent from the scan entirely: UNMEASURED is not FREE, and a header that cannot tell
    those apart is how a pad gets soldered on a guess.
    """

    def test_free_pads_offers_nothing_measured_as_driven(self):
        free = _define_list(HDR, "FREE_PADS")
        assert free is not None, "puc.h has no FREE_PADS"
        bad = sorted(set(free) & DRIVEN_PINS)
        assert not bad, (
            "puc.h offers GPIO %s as free; /scanpu measured them HELD LOW against an internal "
            "pullup, so something external drives the net" % bad)

    def test_free_pads_offers_nothing_carrying_a_clock(self):
        # 38 showed 8 edges at 50% duty in the same scan -- the DS3231 1 Hz. It is an output.
        assert 38 not in (_define_list(HDR, "FREE_PADS") or []), (
            "GPIO38 carries the DS3231's 1 Hz square wave; the scan saw it toggling")

    def test_free_pads_is_exactly_what_the_scan_cleared(self):
        assert set(_define_list(HDR, "FREE_PADS") or []) == FREE_PINS, (
            "FREE_PADS must be the pins the scan actually cleared (%s). Anything the scan did "
            "not reach is UNMEASURED and does not belong in a list called FREE." % sorted(FREE_PINS))
