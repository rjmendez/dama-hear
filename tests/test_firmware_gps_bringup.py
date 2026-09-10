"""The GPS bring-up must not call a link decoded on evidence that noise can produce.

⚠️THIS TEST EXISTS BECAUSE mach LOST 20.2% OF ITS DETECTIONS TO ONE `!=` . Measured 2026-09-10:

  * /pool/corpus/records held 2557 node records for mach, 517 of them unanchored (utc_us == 0)
    against 92 of 2390 for nyquist and 35 of 1299 for rankine.
  * Setting aside the two-or-so rows every node writes in the first 15 s of every boot -- the
    gate arms before any UTC label can exist -- the split is 475 for mach, 5 for nyquist, 15 for
    rankine. mach's loss is a handful of LONG windows, not a thin spread.
  * In those windows the archived health.csv shows a perfect timepulse: pps advancing 1:1 with
    uptime, pps_bad 0, pps_gaps 0, interval spread 2-6 us -- and tacc_ns 0 throughout. tacc_ns is
    assigned from every NAV-PVT of 24 B or more, outside the fix guard, so 1016 consecutive
    seconds of 0 means no NAV-PVT was decoded AT ALL. The module was pulsing and not talking.
    Then 0 -> 19 satellites with tAcc 25 ns inside one 30 s interval: a link coming up, not sky.
  * mach had such a window on 4 of its 7 logged boots, 2401 s in total (3.37% of its logged
    uptime). nyquist: 0 of 3 boots. rankine: 0 of 9. It is the node whose GPS TX/RX pair is
    reversed at the module, and the reversal is handled by gps_bringup's fallback -- which only
    runs `if (!decoded)`.

`gps_autobaud` returned `best_b != 0`: ANY candidate rate that scored a single unit. One unit is
two adjacent bytes reading B5 62, which on a floating pin beside an active line is ~1 in 65k per
byte pair and therefore expected several times across an eight-rate sweep. A spurious count makes
a node with a reversed pair look linked, the swap never runs, and the node keeps a perfect PPS it
cannot name. gps_bit_time_us in the same file already refuses to call a run length a bit time
until it has recurred 20 times, for exactly this reason; the sweep never got that discipline.

The check is on the SOURCE, with comments stripped, because there is no ESP32 toolchain in CI --
tests/test_firmware_csv_schema.py and tests/test_firmware_timebase.py say the same. Comments are
stripped rather than searched: a guard that its own explanation satisfies is not a guard, and the
explanation above names every constant it looks for.
"""
import pathlib
import re

import pytest

INO = pathlib.Path(__file__).resolve().parents[1] / "firmware" / "night_node" / "night_node.ino"


def _source():
    if not INO.exists():
        pytest.skip("night_node.ino not in this checkout")
    t = INO.read_text()
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    return re.sub(r"//[^\n]*", "", t)


def _define(src, name):
    m = re.search(r"#define\s+%s\s+(\d+)" % re.escape(name), src)
    assert m, "%s is not defined" % name
    return int(m.group(1))


def _body(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


# ---------------------------------------------------------------- the sweep's verdict


def test_autobaud_does_not_call_one_count_a_decoded_link():
    """`return best_b != 0;` is the defect. The verdict must come from a confirmation."""
    src = _source()
    body = _body(src, "static bool gps_autobaud()", "\nstatic void gps_bringup()")
    assert "return best_b != 0;" not in body, (
        "a single spurious count still counts as a decoded link -- this is the line that stopped "
        "gps_bringup trying the other pin order on the node whose pair is reversed")
    assert "GPS_DECODE_QUORUM" in body, "the verdict must be a quorum, not a non-zero"


def test_the_confirmation_is_long_enough_for_the_rate_the_module_is_configured_at():
    """⚠️THE WINDOW IS ONLY MEANINGFUL AGAINST THE SOLUTION RATE, and the sketch sets that rate a
    few hundred lines away. gps_configure() asks for K_RATE_MEAS 1000 ms and K_RATE_NAV 1, i.e.
    exactly one solution -- one NAV-PVT and one TIM-TP -- per second. So a quorum of N frames
    cannot be collected in less than N seconds however the constants are written, and a window
    that drifts under that turns a healthy UBX-only node (nyquist measures 1.9 UBX frames/s) into
    one that fails its own confirmation and sweeps for ever. Derived here rather than asserted as
    a number, so moving the nav rate moves the requirement with it."""
    src = _source()
    meas_ms = int(re.search(r"vs_add\(K_RATE_MEAS,\s*(\d+)\)", src).group(1))
    nav_cyc = int(re.search(r"vs_add\(K_RATE_NAV,\s*(\d+)\)", src).group(1))
    period_ms = meas_ms * nav_cyc
    quorum = _define(src, "GPS_DECODE_QUORUM")
    confirm = _define(src, "GPS_CONFIRM_MS")
    assert quorum >= 2, "one frame does not distinguish a link from a glitch"
    assert confirm >= quorum * period_ms, (
        "GPS_CONFIRM_MS %d ms cannot carry %d frames at one per %d ms"
        % (confirm, quorum, period_ms))


def test_the_pin_probe_outlasts_one_transmit_period():
    """⚠️250 ms WAS SHORTER THAN THE THING IT LOOKED FOR. At one solution per second the module
    bursts for a few tens of ms and is idle for the rest of the second, so a 250 ms window misses
    a healthy, talking module about three times in four -- and a miss on both pins is `module
    silent, using documented wiring`, which on mach is the pinout it does not have."""
    src = _source()
    period_ms = (int(re.search(r"vs_add\(K_RATE_MEAS,\s*(\d+)\)", src).group(1))
                 * int(re.search(r"vs_add\(K_RATE_NAV,\s*(\d+)\)", src).group(1)))
    probe = _define(src, "GPS_PIN_PROBE_MS")
    assert probe >= period_ms, (
        "a %d ms probe cannot be sure of seeing a burst that comes every %d ms"
        % (probe, period_ms))
    body = _body(src, "static void gps_pick_pins()", "\nstatic void i2c_scan()")
    assert "GPS_PIN_PROBE_MS" in body and "250)" not in body, (
        "gps_pick_pins must use the named window on BOTH pins")


# ---------------------------------------------------------------- the watchdog


def _watchdog(src):
    """Anchored on the cadence table, not on the comment heading it: comments are stripped."""
    return _body(src, "static const uint32_t GPS_RETRY_MS[]", "gps_bringup();")


def test_the_retry_cadence_does_not_cost_two_minutes_per_attempt():
    """A flat 120 s retry charges every failed attempt two minutes of untimed detections, and
    mach's 1016 s silent window is about eight of them end to end. The first retry has to be far
    inside the old 45 s arming delay; the later ones may back off."""
    src = _source()
    w = _watchdog(src)
    waits = [int(x) for x in re.findall(r"(\d+)UL", w)]
    assert waits, "the watchdog no longer states its own cadence in ms"
    assert min(waits) <= 20000, (
        "the first retry is %d ms in; it needs to be early, that is where the value is"
        % min(waits))
    assert sorted(waits) == waits or len(set(waits)) > 1, "a cadence, not one number"
    assert max(waits) >= 60000, "it must back off, or an unpowered module sweeps for ever"


def test_the_watchdog_re_arms_on_a_link_that_went_quiet_after_working():
    """⚠️`nothing has EVER decoded` disarms permanently on the first good frame. A module that
    resets, or a UART that drops after one frame, then leaves the node with a perfect timepulse,
    no NAV-PVT, and nothing that will ever look again. NAV-PVT arrives with or without a fix, so
    a run of seconds with the pulse advancing and no NAV-PVT is the link, never the sky."""
    w = _watchdog(_source())
    assert "ubx_silent_run" in w, (
        "the watchdog must also fire on a proven link that stopped delivering NAV-PVT")


# ---------------------------------------------------------------- the counters


def test_the_loss_is_counted_where_it_happens():
    """⚠️label_rejects (time_glitch) COUNTS ONE BRANCH AND WAS READ AS THE LOSS. It increments
    only when a NAV-PVT's second fails to advance one-per-edge; it counts nothing when no NAV-PVT
    arrives at all, which is every second of the windows above. Over the same archive it read 50
    on mach and 135 on rankine -- ranking the node that lost 517 rows better than the one that
    lost 35."""
    src = _source()
    assert re.search(r"time_glitch\+\+", src), "time_glitch still has exactly one increment site"
    assert len(re.findall(r"time_glitch\+\+", src)) == 1, (
        "time_glitch gained an increment; it is a single-branch counter by definition and the "
        "new site needs its own name")
    assert re.search(r"if\s*\(!tok\)\s*dets_unlabelled\+\+", src), (
        "a detection written with utc_us == 0 must be counted at the point it is written")


def test_the_new_counters_reach_both_places_a_reader_can_see_them():
    """/status for a node that is still in the window, health.csv for one that has closed. mach's
    two longest windows were both over by the time anyone looked."""
    src = _source()
    for name in ("dets_unlabelled", "first_label_s", "ubx_silent_s", "ubx_silent_max_s"):
        assert '\\"%s\\"' % name in src, "/status must report %s" % name
    hdr = src[src.index("HEALTH_HDR[]"):]
    hdr = hdr[:hdr.index(";")]
    for name in ("ubx_pvt", "dets_unlabelled", "first_label_s", "ubx_silent_max"):
        assert name in hdr, "health.csv must carry %s" % name


def test_ubx_silence_excludes_the_seconds_a_probe_took_the_uart_away():
    """A bring-up sweep detaches the UART for ~11 s on purpose. Charging a diagnostic's own
    disturbance as an outage is what once made this node report a 3.95 SECOND pps spread; every
    probe route in this file already declares its disturbance, and so must this counter."""
    src = _source()
    i = src.index("ubx_silent_run += span")
    blk = src[i - 500:i]
    assert "!probed" in blk, "the silence counter must exclude probed seconds"
