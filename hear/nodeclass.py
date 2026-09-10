#!/usr/bin/env python3
"""What a node's hardware can and cannot contribute, as data the solvers can refuse on.

WHY THIS EXISTS. `hear/backend/survey.py` answers "where is node 7". Nothing answered "can node 7
produce an arrival time at all", and the honest answer is not always yes. A BirdWeather PUC has two
microphones, a full environmental suite and an on-device classifier, and it is a genuinely better
listener than the XIAO nodes -- but if its clock carries milliseconds of error, its timestamps in a
TDoA solve are not a worse measurement, they are a different measurement wearing the same units.

The number that makes this concrete: sound travels 343 mm in a millisecond. A node disciplined to
GPS PPS carries tens of microseconds of timestamp error (measured live on all three nodes
2026-09-10 22:09 UTC: GPS tAcc 28/31/38 ns, PPS interval spread 20/14/46 us for mach/nyquist/
rankine). A node timed by ordinary phone-grade sync carries units of milliseconds -- dama-gotchi's
GPSTimingSync.java:45 pins its "location" tier at 5 ms, which is 1.7 m. Averaging the two into one
solve does not split the difference; it takes the worse one and hides it behind a residual that
still looks fine, because a common-mode timing error moves every range together.

So capability is declared per class, checked at the door, and REFUSED rather than degraded --
the same discipline as SurveyError raising at load time instead of decorating every later answer.

⚠️THE GATE TESTS NUMBERS, NOT LABELS. This file used to admit an arrival on
`time_source == "gps_pps"` -- a string comparison. That refused a receiver for the name of its
clock source rather than for the size of its error, so a chrony-disciplined host measured at 124 us
would have been inadmissible while a class that merely *called itself* "gps_pps" was waved through.
`contributes_arrival()` is now two measured predicates, both required:

    clock_admissible()      t_sigma_s within ARRIVAL_T_SIGMA_MAX_S
    capture_bias_bounded()  path_bias_s STATED and within ARRIVAL_PATH_BIAS_MAX_S

⚠️THE SECOND PREDICATE IS WHY THE FIRST IS NOT A LOOPHOLE. A good clock is necessary and it is
nowhere near sufficient. Between the wavefront reaching the diaphragm and the number the firmware
writes down sits an analogue-to-digital path -- a decimation filter, a DMA block, an ALSA/USB
buffer, an Android HAL -- whose delay is a BIAS: a constant per-receiver offset that

  * does not average down over events, however many events are collected;
  * is INVISIBLE in a 3-node fit, because two independent TDoAs exactly determine (x, y) with z
    fixed, so the residual is identically zero by construction and cannot report it;
  * moves that receiver's range directly, at 343 mm per millisecond.

That, and not the name of its clock, is the reason a phone or an ALSA-captured host is out today.
A class may state a `path_bias_s` only if someone MEASURED the delay against an external
reference. `None` means never measured, and never-measured is a refusal.

⚠️WHAT THIS MODEL DOES NOT DO. `path_bias_s` is per class and is treated as if it never cancels.
In a solve between two receivers of the SAME class an identical capture path does cancel, so the
gate is conservative there; between DIFFERENT classes it does not cancel at all, and that
difference is not represented. Adding a receiver of a new class is therefore the case this file is
sized for, and it is the case where an unmeasured path stops being harmless.

⚠️A CLASS DESCRIBES HARDWARE, NOT A PROMISE. `xiao-s3-pps` says a node of that class HAS a PPS
input wired to D0. It does not say the GPS is talking: measured live 2026-09-10, nyquist and
rankine both report `valid_nmea: 0` at baud 230400 while decoding UBX normally (`ubx_pvt` 8906 and
8872, fix 3), and mach -- which an older revision of this comment named as the broken one -- is
decoding 154023 of 182688 sentences at 115200. Use the class to decide what a node could
contribute and the node's own telemetry to decide what it did.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

# Speed of sound at 20 C, only used to turn a timing error into a distance for the messages below.
# The live value comes from a node's own BMP280 (hear reports env.c_mps); this is for exposition.
_C_NOMINAL_MPS = 343.0

# ---------------------------------------------------------------- the arrival budget
# ONE sourced number, and everything below is arithmetic on it. docs/node-hardware.md, "The BME280
# is an acoustic sensor here": `c = 331.3 + 0.606 * T`, so one degree of unmeasured air temperature
# is 0.606 m/s, which over the array's ~35 m working scale is
#     dt = d * dc / c**2 = 35 * 0.606 / 343**2 = 1.80e-4 s
# -- the 183 us that file states, and that hear/node/detect.py, hear/node/telemetry.py and
# tools/hear_latency_cal.py all already argue against. It is the size of an error the design
# tolerates from a sensor it cannot fully remove, so it is the honest ceiling for a term it CAN
# choose to refuse. Refusing something an order below it would be theatre; admitting something an
# order above it would make the temperature work pointless.
ARRIVAL_ONE_WAY_BUDGET_S = 183e-6

# WHY NOT survey.json's TIGHTEST PAIR. The alternative anchor was the shortest baseline the array
# actually has -- nyquist->rankine, sqrt(4.58**2 + 10.48**2 + 3.00**2) = 11.82 m, against which
# 183 us (62.8 mm) is 0.53%. It was considered and REJECTED as the threshold for three reasons:
# it is site geometry, so moving one node would silently change which HARDWARE is admissible;
# those coordinates carry sigma_m 0.49-0.72 m, which is 8-11x the budget being set, so the
# threshold would be less certain than the thing it gates; and two of the three heights are
# "nominal storey, NOT measured". The survey is used here only as the cross-check in the previous
# sentence. A gate must test the receiver, not the parking spot.

# A TDoA is a DIFFERENCE of two arrivals, so a PER-NODE threshold is the one-way budget divided by
# the way that node's error combines with its partner's -- and the two kinds combine differently:
#
#   random (t_sigma_s)    independent between receivers -> RSS -> the pair carries sqrt(2)*sigma
#   bias   (path_bias_s)  deterministic; worst case the two offsets have opposite signs and add
#                         -> the pair carries 2*bias
#
# Hence the bias bound is the TIGHTER of the two despite coming from the same budget. That is the
# correct direction anyway: scatter averages down over events and a bias does not.
ARRIVAL_T_SIGMA_MAX_S = ARRIVAL_ONE_WAY_BUDGET_S / math.sqrt(2.0)   # 129.4 us = 44.4 mm
ARRIVAL_PATH_BIAS_MAX_S = ARRIVAL_ONE_WAY_BUDGET_S / 2.0            # 91.5 us = 31.4 mm


class CapabilityError(ValueError):
    """A node was asked for something its hardware cannot do. ValueError subclass so a caller that
    already catches the repo's refusals catches this too."""


class NodeClass:
    """One hardware configuration.

    Fields are deliberately about MEASURABLE hardware limits, not about intent:

    time_source     "gps_pps" | "ntp" | "none"
                    ⚠️NOT the eligibility test -- see contributes_arrival(). It is documentation,
                    plus ONE load-bearing use: "none" means nothing disciplines this receiver's
                    clock to UTC at all, so `t_sigma_s` is a guess about a free-running oscillator
                    rather than a measurement of a disciplined one, and no sigma it states can be
                    believed. That is a floor, not a label check.
    t_sigma_s       RANDOM timestamp uncertainty, seconds, 1-sigma: the part that averages down.
                    This is the number a solver should propagate as noise.
    path_bias_s     The part of the capture-path delay that is NOT corrected before the timestamp
                    is emitted, seconds, as a magnitude. `None` means NEVER MEASURED, which is a
                    REFUSAL and not a zero -- see the module docstring. Exactly 0.0 is rejected at
                    construction for the same reason `t_sigma_s = 0` is: no hardware supports the
                    claim.
    mic_count       2 or more permits an on-node bearing from the fixed baseline; 1 does not.
    fs_hz           Sample rate. Nyquist is fs/2 and no filter, model or integration time
                    recovers anything above it.
    band_hz         (lo, hi) the MICROPHONE passes, before Nyquist is applied. The tighter of
                    this and Nyquist is what the node can actually hear -- see usable_band_hz().
    env             Sensors present, as a set of names. "temp" is the one that matters: sound
                    speed moves 0.6 m/s per degree and biases every node the same way, so it
                    does NOT cancel in TDoA.
    raw_retain_s    Seconds of raw audio retrievable on request, 0 if none.
    notes           Free text for whoever reads a refusal message.
    """

    def __init__(self, name: str, time_source: str, t_sigma_s: float, mic_count: int,
                 fs_hz: float, band_hz: Sequence[float], env: Sequence[str] = (),
                 raw_retain_s: float = 0.0, notes: str = "",
                 path_bias_s: Optional[float] = None) -> None:
        if time_source not in ("gps_pps", "ntp", "none"):
            raise CapabilityError("unknown time_source %r" % (time_source,))
        if mic_count < 1:
            raise CapabilityError("%s: mic_count must be >= 1" % name)
        if fs_hz <= 0:
            raise CapabilityError("%s: fs_hz must be positive" % name)
        lo, hi = float(band_hz[0]), float(band_hz[1])
        if not lo < hi:
            raise CapabilityError("%s: band_hz must be (lo, hi) with lo < hi" % name)
        if t_sigma_s <= 0:
            raise CapabilityError("%s: t_sigma_s must be positive -- a node with no timing error "
                                  "is a claim no hardware supports" % name)
        if path_bias_s is not None:
            path_bias_s = float(path_bias_s)
            if not math.isfinite(path_bias_s) or path_bias_s <= 0.0:
                raise CapabilityError(
                    "%s: path_bias_s must be a positive finite number of seconds, or None. None "
                    "means the capture-path delay was never measured, which is a refusal; %r "
                    "claims a capture path with no residual offset, which no hardware supports."
                    % (name, path_bias_s))
        self.name = name
        self.time_source = time_source
        self.t_sigma_s = float(t_sigma_s)
        self.path_bias_s = path_bias_s
        self.mic_count = int(mic_count)
        self.fs_hz = float(fs_hz)
        self.band_hz = (lo, hi)
        self.env = frozenset(env)
        self.raw_retain_s = float(raw_retain_s)
        self.notes = notes

    # ---- what follows from the fields ------------------------------------------------------
    @property
    def nyquist_hz(self) -> float:
        return self.fs_hz / 2.0

    def usable_band_hz(self):
        """(lo, hi, limit) -- the band this node can actually deliver, and WHICH ceiling bound it.

        `limit` is "microphone" or "nyquist", and they need different remedies: a Nyquist ceiling
        is fixed by sampling faster, a microphone ceiling is fixed only by a different part. The
        distinction is not pedantic -- the XIAO PDM nodes are microphone-bound at 10 kHz, and the
        planned ICS-43434 would be microphone-bound at 15 kHz.
        """
        hi, limit = self.band_hz[1], "microphone"
        if self.nyquist_hz < hi:
            hi, limit = self.nyquist_hz, "nyquist"
        return self.band_hz[0], hi, limit

    def range_sigma_m(self, c_mps: float = _C_NOMINAL_MPS) -> float:
        """The RANDOM timestamp error expressed as distance. Half the honest cost of a class."""
        return self.t_sigma_s * float(c_mps)

    def path_bias_m(self, c_mps: float = _C_NOMINAL_MPS) -> Optional[float]:
        """The uncorrected capture-path delay as distance, or None if it was never measured.

        The other half of the cost, and the half a residual cannot show you.
        """
        if self.path_bias_s is None:
            return None
        return self.path_bias_s * float(c_mps)

    def can_hear(self, f_lo_hz: float, f_hi_hz: float) -> bool:
        """Does the requested band lie wholly inside what this node delivers?"""
        lo, hi, _ = self.usable_band_hz()
        return f_lo_hz >= lo and f_hi_hz <= hi

    def can_bear(self) -> bool:
        """Can this node produce a bearing on its own? Needs at least two microphones."""
        return self.mic_count >= 2

    # ---- the two arrival predicates ---------------------------------------------------------
    def clock_admissible(self) -> bool:
        """Is this receiver's CLOCK good enough for its timestamps to be arrivals?

        A measured test, with one string check that is genuinely load-bearing: `time_source`
        "none" says nothing disciplines this clock to UTC, so whatever `t_sigma_s` it states is
        not a measurement of a synchronised clock and there is nothing to compare against the
        threshold. Every other source name -- "gps_pps", "ntp" -- is documentation, and a
        receiver disciplined by any means that MEASURES small enough passes.
        """
        if self.time_source == "none":
            return False
        return self.t_sigma_s <= ARRIVAL_T_SIGMA_MAX_S

    def capture_bias_bounded(self) -> bool:
        """Has the capture path's fixed delay been MEASURED, and is what remains small enough?

        Separate from clock_admissible() on purpose. These two fail for unrelated reasons, and a
        caller told only "refused" cannot tell whether to fix a clock or a microphone driver.
        Unmeasured is False: see the module docstring on why a bias cannot be recovered from a
        residual after the fact.
        """
        if self.path_bias_s is None:
            return False
        return self.path_bias_s <= ARRIVAL_PATH_BIAS_MAX_S

    def arrival_refusals(self, c_mps: float = _C_NOMINAL_MPS) -> List[str]:
        """Every reason this class is not an arrival source, as sentences with numbers in them.

        contributes_arrival() and require_arrival()'s message are both built from this one list,
        so the gate and its explanation cannot drift apart.
        """
        out: List[str] = []
        if self.time_source == "none":
            out.append(
                "nothing disciplines its clock to UTC (time_source \"none\"), so its stated "
                "t_sigma of %.3f ms is not a measurement of a synchronised clock"
                % (self.t_sigma_s * 1e3,))
        elif self.t_sigma_s > ARRIVAL_T_SIGMA_MAX_S:
            out.append(
                "clock t_sigma %.1f us = %.3f m of range exceeds the %.1f us per-node bound "
                "(%.0f us one-way budget / sqrt(2), because two independent clocks combine in RSS)"
                % (self.t_sigma_s * 1e6, self.range_sigma_m(c_mps),
                   ARRIVAL_T_SIGMA_MAX_S * 1e6, ARRIVAL_ONE_WAY_BUDGET_S * 1e6))
        if self.path_bias_s is None:
            out.append(
                "its capture-path delay has NEVER BEEN MEASURED (path_bias_s is None). A bias "
                "does not average down and is invisible in a 3-node exactly-determined fit, "
                "whose residual is identically zero by construction, so it cannot be discovered "
                "later -- it has to be measured against an external reference first")
        elif self.path_bias_s > ARRIVAL_PATH_BIAS_MAX_S:
            out.append(
                "its uncorrected capture-path delay %.1f us = %.3f m of range exceeds the "
                "%.1f us per-node bound (%.0f us one-way budget / 2, because two biases can add)"
                % (self.path_bias_s * 1e6, self.path_bias_m(c_mps),
                   ARRIVAL_PATH_BIAS_MAX_S * 1e6, ARRIVAL_ONE_WAY_BUDGET_S * 1e6))
        return out

    def contributes_arrival(self) -> bool:
        """Is this node's timestamp admissible as a TDoA arrival at all?

        Both predicates, and no shortcut: a receiver with an excellent clock and an unmeasured
        audio path is refused, because the error it would inject is exactly the kind this array's
        geometry cannot detect.
        """
        return self.clock_admissible() and self.capture_bias_bounded()

    def __repr__(self) -> str:
        lo, hi, lim = self.usable_band_hz()
        bias = ("bias unmeasured" if self.path_bias_s is None
                else "bias %.0f us" % (self.path_bias_s * 1e6,))
        return ("NodeClass(%s, %s, t_sigma=%.0f us = %.2f m, %s, %d mic, %.0f-%.0f Hz [%s])"
                % (self.name, self.time_source, self.t_sigma_s * 1e6,
                   self.range_sigma_m(), bias, self.mic_count, lo, hi, lim))


# ---------------------------------------------------------------- the registry
# Every figure below is either measured on the hardware or taken from the part's datasheet, and
# says which. A class whose numbers are guesses would defeat the point of having the class.
CLASSES: Dict[str, NodeClass] = {}


def register(cls: NodeClass) -> NodeClass:
    if cls.name in CLASSES:
        raise CapabilityError("class %r already registered" % cls.name)
    CLASSES[cls.name] = cls
    return cls


register(NodeClass(
    name="xiao-s3-pps",
    time_source="gps_pps",
    # MEASURED on nyquist over the 2026-09-07 11.33 h capture: GPS tAcc 22-26 ns, PPS interval
    # spread 10-15 us, zero glitches in 40825 edges. Re-checked live on all three nodes
    # 2026-09-10 22:09 UTC: tAcc 28/31/38 ns and PPS spread 20/14/46 us (mach/nyquist/rankine),
    # zero glitches on all three. The dominant term is NOT the GPS -- it is the 256-sample I2S
    # block, which the firmware back-dates but only to about one sample. 100 us is a deliberately
    # conservative round number above the parts that have been measured; it has never been checked
    # against an external reference because the node has no second clock.
    #
    # ⚠️THE BUDGET HOLDS ON THE DETECTION PATH AND USED TO FAIL BY 11x ON THE RAW-RING PATH.
    # Termwise RSS for a dets.csv arrival, 2026-09-08: GPS tAcc 0.02 us, PPS spread/2 5.00 us,
    # esp_timer between anchors 12.20 us (9.5-12.2 ppm vs GPS, re-zeroed every PPS second),
    # I2S block-quantisation residual ~1 sample 62.47 us, fs_clean back-date differential
    # 8.90 us -> 64.47 us = 22.1 mm. It fits, and the term that dominates it is the one this
    # comment already named. Recomputed with rankine's worse live spread (46 us, so spread/2 =
    # 23 us) it is 68.3 us -- still inside 100 us, so the constant covers the worst of the three.
    #
    # The RING is a different path and was NOT inside this number. sample_to_utc interpolates
    # from the nearest PPS mark with fs_clean as the slope, and fs_clean is block-quantised to
    # 16000/win_s ppm, so two nodes 1118 ppm apart carried 1118 us = 0.384 m of differential
    # error at the far end of a one-second mark gap, and a full 30 s /audio window pulled from
    # both differed by 33-41 ms = 11-14 m. Round-tripping through X-Audio-From-Utc-Us does not
    # help: the node computes it with the same wrong slope. The firmware now refuses to use
    # fs_clean as a timebase until its window supports 100 ppm (FS_TIMEBASE_MIN_WIN_S = 160 s,
    # derived from THIS constant -- tests/test_firmware_timebase.py holds the two together), and
    # falls back to the nominal rate below that, which is wrong identically on every node and
    # cancels in a TDoA. See docs/timing.md for the full budget and what is still open.
    t_sigma_s=100e-6,
    # THE CAPTURE PATH IS CORRECTED, WHICH IS WHY THIS CLASS PASSES THE BIAS PREDICATE AND THE
    # OTHERS DO NOT. docs/timing.md, "The two paths, which are not the same path": the detection
    # timestamp is `utc = local_to_utc(esp_timer_get_time() - back_us)` with
    # `back_us = (n-1-i)*1e6/fs_clean`, which moves the stamp back off the block boundary onto the
    # sample that actually carried the peak. The DMA block delay is subtracted rather than carried,
    # and what is left is the quantisation of that correction: one sample period, 1/16000 =
    # 62.5 us, matching the 62.47 us the timing budget measures for the same term.
    #
    # ⚠️THIS IS DELIBERATELY DOUBLE-COUNTED. The same 62.47 us is already one of the RSS terms
    # inside t_sigma_s above. Charging it again here is the conservative direction for a gate and
    # is cheaper than arguing about which half of it is random.
    #
    # ⚠️WHAT IS NOT IN IT: the PDM microphone's own decimation-filter group delay, which nothing in
    # this repo has measured. It is identical on every node of this class, so it cancels in a
    # xiao-to-xiao TDoA and this array has never seen it -- and it would NOT cancel against a
    # receiver of any other class. A fourth receiver of a different class needs that number
    # measured on BOTH sides, not just on its own.
    path_bias_s=1.0 / 16000.0,
    mic_count=1,
    fs_hz=48000.0,
    # The XIAO's onboard PDM MEMS part. The low edge is where the firmware's DC block sits (1.6 Hz);
    # the datasheet's response ends at 10 kHz, so at 48 kHz the microphone binds, not Nyquist.
    band_hz=(50.0, 10000.0),
    env=("temp", "press"),
    raw_retain_s=80.0,   # 7.68 MB of PSRAM at 48 kHz; firmware before hear_node stepped to 60 s
    notes="XIAO ESP32-S3 Sense + u-blox GPS on D0 PPS + BMP280 + microSD. nyquist, mach, rankine "
          "-- all three answered GET /status with this class 2026-09-10.",
))

register(NodeClass(
    name="xiao-s3-i2s",
    time_source="gps_pps",
    t_sigma_s=100e-6,
    # NOT BUILT, so nothing has been measured through it. The back-date argument that earns
    # xiao-s3-pps its number is about firmware this variant does not yet run, on hardware that
    # does not yet exist, and inheriting a measurement across a change of microphone technology
    # (PDM to I2S: different decimation, different group delay) is exactly the transplant that
    # makes a constant wrong while it still looks right. None until someone measures it.
    path_bias_s=None,
    mic_count=1,
    fs_hz=48000.0,
    # ICS-43434, docs/node-hardware.md "What the ICS-43434 can and cannot do": 50 Hz - 15 kHz,
    # low-passed above 24 kHz, NO ultrasonic content at any sample rate. At 48 kHz the microphone
    # binds rather than Nyquist, which is the whole reason that section exists.
    band_hz=(50.0, 15000.0),
    env=("temp", "press"),
    raw_retain_s=80.0,   # same PSRAM budget, three times the rate
    notes="Planned I2S variant. Not built. Bandwidth-limited by the part, not by the sample rate. "
          "Refused for arrivals ONLY because its capture path has never been measured; its clock "
          "budget would pass.",
))

register(NodeClass(
    name="puc-pps",
    time_source="gps_pps",
    # PROVISIONAL. Same order as the XIAO nodes IF the GPS PPS reaches a GPIO -- unverified, and
    # the single fact that decides whether this class exists at all. (An earlier revision pointed
    # at docs/node-classes.md for this; `git log --all -- docs/node-classes.md` is empty, so that
    # file has never existed in this repo. What would settle it is a scope on the PUC's ESP32-S3
    # or a teardown, and neither has been done.)
    t_sigma_s=100e-6,
    # ⚠️THIS CLASS USED TO BE ADMITTED AS AN ARRIVAL SOURCE AND IS NOT ANY MORE. Nothing about the
    # hardware changed; the gate stopped taking a class's word for it. The PUC is closed firmware
    # whose audio path nobody here has instrumented, so its capture delay is unmeasured -- and
    # under the old string test that fact could not be expressed, because "gps_pps" alone let it
    # through. Admitting it would have put an unknown constant offset into a solve whose residual
    # is identically zero by construction. Measure the PUC's path against a co-located
    # xiao-s3-pps -- tools/hear_latency_cal.py already does exactly this subtraction for phones --
    # and put the number here.
    path_bias_s=None,
    mic_count=2,
    fs_hz=48000.0,
    band_hz=(50.0, 15000.0),
    env=("temp", "humidity", "press", "voc", "co2", "light"),
    raw_retain_s=0.0,
    notes="BirdWeather PUC, IF its GPS PPS is wired to the ESP32-S3. UNVERIFIED -- do not survey a "
          "PUC as this class until a scope or a teardown confirms the PPS pin, and do not expect "
          "arrivals from it until its capture-path delay is measured.",
))

register(NodeClass(
    name="puc-ntp",
    time_source="ntp",
    # dama-gotchi's GPSTimingSync docstring claims 1-5 ms cross-device sync for the same class of
    # (UTC, monotonic) anchoring. 3 ms is the middle of that, and it is 1.0 m of range -- 23x the
    # 129.4 us per-node clock bound. This class is admissible for classification and environment
    # and is REFUSED for arrivals.
    #
    # ⚠️NOTE WHAT CHANGED AND WHAT DID NOT. The refusal no longer depends on the word "ntp". If a
    # PUC were disciplined by NTP to a MEASURED tens of microseconds -- which PTP, or a LAN-local
    # chrony source, genuinely can do -- the clock predicate would admit it, and it would still
    # need its capture path measured before it produced an arrival. The old test refused this
    # class for its label. This one refuses it for its 3 ms.
    t_sigma_s=3e-3,
    path_bias_s=None,
    mic_count=2,
    fs_hz=48000.0,
    band_hz=(50.0, 15000.0),
    env=("temp", "humidity", "press", "voc", "co2", "light"),
    raw_retain_s=0.0,
    notes="BirdWeather PUC timed by NTP and its RTC. An RTC gives holdover, not sync. Excellent "
          "listener, not a ranging node.",
))


register(NodeClass(
    name="gotchi-phone",
    time_source="ntp",
    # ⚠️THIS ENTRY WAS SUBSTANTIALLY STALE AND IS REWRITTEN AGAINST THE SOURCE, 2026-09-10. It used
    # to declare time_source "none" on the grounds that "the audio path never asks the clock what
    # time it is", cite 44100 Hz, cite System.nanoTime(), and conclude "There is no arrival time
    # leaving the phone today at all". Every one of those is now false. Checked in
    # /home/rjmendez/dama-gotchi:
    #
    #   fs                  AcousticAntCollector.java:56 SAMPLE_RATE = 48_000, not 44100. The old
    #                       "HOP_SIZE 1024 @ 44100 Hz = 23.2 ms" arithmetic was on the wrong rate
    #                       (1024/48000 is 21.33 ms) and is no longer what dates an onset anyway.
    #   HAL anchor          AcousticRangingCollector.kt:3142 calls
    #                       `rec.getTimestamp(ts, AudioTimestamp.TIMEBASE_BOOTTIME)` and anchors
    #                       the capture ring's frame axis on it (capAnchorFrame / capAnchorBootNs,
    #                       :3144-3146). AcousticAntCollector consumes it through setFrameClock
    #                       (:904), wired at DeviceMetricsPoller.java:1689. The old claim
    #                       "AudioRecord.getTimestamp() is never called anywhere in the app" is
    #                       false.
    #   clock               feedSamples (:906-916) stamps on SystemClock.elapsedRealtimeNanos(),
    #                       CLOCK_BOOTTIME -- nanoTime() was removed because it stops advancing in
    #                       deep sleep. GPSTimingSync.java:45 pins the "location" tier at 5 ms.
    #   arrival on the wire DeviceMetricsPoller.java:1588 installs a FIVE-argument impulse callback
    #                       `(peakDb, riseDb, ns, sigmaNs, clipFrac)` and passes all five to
    #                       AcousticEnvRecorder.noteImpulse. Only the TAK/CoT branch drops `ns`,
    #                       and a CoT detection is not an arrival. The sketch payload carries
    #                       onset_offset_us, onset_dated, onset_found, clock_tier and
    #                       sync_sigma_ns, and hear/pool.py reads all five into the pool record.
    #   raw PCM             AudioCaptureRing of CAPTURE_RING_SAMPLES = ROUND_SAMPLES*8 = 384000 at
    #                       48 kHz = 8.0 s (AcousticRangingCollector.kt:258, :2910), served by
    #                       AudioPullResponder (:2986) on dama/colony/audio/pull. The old claim
    #                       "no raw PCM is retained, so cross-correlation against a node is not
    #                       possible either" is false -- and cross-correlation is the obvious way
    #                       to measure the bias below.
    #
    # t_sigma_s is now the CLOCK term and only the clock term: GPSTimingSync's own location-tier
    # figure, LOCATION_TIER_SIGMA_NS = 5_000_000 ns (GPSTimingSync.java:45), mirrored in
    # dama-gotchi/sensors/tdoa_triangulation.py's TIER_DEFAULT_SIGMA_NS. 5 ms is 1.7 m and 39x the
    # 129.4 us bound, so the phone is still refused on its clock -- but by a number it publishes
    # itself in sync_sigma_ns, so a phone that ever reports better is not held back by this
    # entry's history.
    t_sigma_s=5e-3,
    # THE DISQUALIFYING TERM, AND THE ONE THIS FILE EXISTS TO NAME.
    # dama-gotchi/android/app/src/main/assets/acoustic_latency_calibration.json, read 2026-09-10:
    #     myasshurts-9669aa0e         13_122_000 ns = 13.122 ms
    #     financialdistress-a4a491b0 293_499_000 ns = 293.499 ms
    #     by_model                    {}
    # Three phones, two entries, and the second is past the app's own
    # MAX_PLAUSIBLE_LATENCY_OFFSET_NS = 100 ms, so it is refused on-device and never applied. With
    # by_model empty the model fallback cannot fire either, so two of three phones apply NO
    # correction at all and carry their whole path delay into every arrival.
    #
    # 13.122 ms is therefore the BEST of the fleet and not the typical one, and even that best
    # case is 4.50 m of range: 143x the 91.5 us bias bound. It is stated here rather than left as
    # None because it is a real measurement, and a refusal that quotes a measured 4.50 m is worth
    # more than one that says "unknown". tools/hear_latency_cal.py is the tool that produces these
    # numbers from a co-located PPS node, and its own warning applies -- the co-location is the
    # operator's assumption, and every metre of separation is 2.9 ms of pure bias in the answer.
    path_bias_s=13.122e-3,
    mic_count=1,
    fs_hz=48000.0,
    # UNSOURCED, inherited from the previous revision of this entry: no datasheet for the handset
    # MEMS parts has been consulted and no sweep has been run. At 48 kHz Nyquist is 24 kHz, so
    # this bound is what usable_band_hz() reports, and it is the one figure in this class that
    # nobody has checked.
    band_hz=(50.0, 20000.0),
    env=("temp", "press"),
    raw_retain_s=8.0,
    notes="dama-gotchi Android node. EXCELLENT sensor platform: a 48 kHz microphone wider than any "
          "XIAO node, an 8 s raw ring it will serve on request, a HAL-anchored frame axis and an "
          "onset that reaches the pool. Refused for arrivals on TWO counts -- a 5 ms clock, and an "
          "uncorrected 13.1 ms capture-path latency on the best of three devices. Fix the latency "
          "table (tools/hear_latency_cal.py) and the bias goes; the clock needs GPSTimingSync on a "
          "better tier than \"location\".",
))


def get(name: str) -> NodeClass:
    try:
        return CLASSES[name]
    except KeyError:
        raise CapabilityError("unknown node class %r (have %s)"
                              % (name, ", ".join(sorted(CLASSES))))


# ---------------------------------------------------------------- the door
def require_arrival(name: str, node_id=None, c_mps: float = _C_NOMINAL_MPS) -> NodeClass:
    """Admit a class as a source of TDoA arrivals, or raise saying what it would have cost.

    This is the function that makes the model worth having. A caller that skips it and averages a
    millisecond-class receiver into a microsecond-class solve gets an answer with a plausible
    residual and a metre of common-mode error, which no later check can find.

    The message names EVERY failing predicate, not just the first. A receiver refused for its
    clock is usually also unmeasured on its capture path, and reporting one at a time turns a
    single fix into two round trips through the field.
    """
    cls = get(name)
    why = cls.arrival_refusals(c_mps)
    if not why:
        return cls
    where = "" if node_id is None else " (node %r)" % (node_id,)
    raise CapabilityError(
        "class %r%s is not a source of TDoA arrivals, against a %.0f us one-way budget "
        "(docs/node-hardware.md: one degree of unmeasured air temperature over 35 m): %s. "
        "Use it for classification, bearing and environment instead."
        % (cls.name, where, ARRIVAL_ONE_WAY_BUDGET_S * 1e6, "; ".join(why)))


def require_band(name: str, f_lo_hz: float, f_hi_hz: float, node_id=None) -> NodeClass:
    """Admit a class for a band, or raise saying WHICH ceiling stopped it and what fixes it."""
    cls = get(name)
    if cls.can_hear(f_lo_hz, f_hi_hz):
        return cls
    lo, hi, limit = cls.usable_band_hz()
    where = "" if node_id is None else " (node %r)" % (node_id,)
    remedy = ("sample faster" if limit == "nyquist"
              else "a different microphone; no sample rate fixes this")
    raise CapabilityError(
        "class %r%s delivers %.0f-%.0f Hz (%s-limited), so it cannot supply %.0f-%.0f Hz. "
        "Remedy: %s." % (cls.name, where, lo, hi, limit, f_lo_hz, f_hi_hz, remedy))


def timing_budget_m(names: Sequence[str], c_mps: float = _C_NOMINAL_MPS) -> Dict[str, float]:
    """Per-class RANDOM range error for a set of classes, so a mixed network's weakest link is
    visible before it is averaged into an answer rather than after.

    ⚠️This is the sigma only. A class can score well here and still be inadmissible because its
    capture-path bias is unmeasured, which is the error a residual cannot show -- see
    arrival_budget_m() for both halves.
    """
    return {n: get(n).range_sigma_m(c_mps) for n in dict.fromkeys(names)}


def arrival_budget_m(names: Sequence[str],
                     c_mps: float = _C_NOMINAL_MPS) -> Dict[str, Dict[str, Optional[float]]]:
    """Both halves of each class's cost, in metres, plus whether it is admissible at all.

    `bias_m` is None where the capture path was never measured. None is NOT zero and must not be
    summed as if it were; a caller that wants one number out of this should refuse the class.
    """
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for n in dict.fromkeys(names):
        c = get(n)
        out[n] = {"sigma_m": c.range_sigma_m(c_mps),
                  "bias_m": c.path_bias_m(c_mps),
                  "admissible": c.contributes_arrival()}
    return out


def describe(names: Optional[Sequence[str]] = None) -> str:
    """A table for a human. Kept here so the refusal messages and the documentation cannot drift."""
    rows = [get(n) for n in (names if names is not None else sorted(CLASSES))]
    out = ["arrival gate: t_sigma <= %.1f us AND a MEASURED capture bias <= %.1f us "
           "(%.0f us one-way budget, / sqrt(2) and / 2)"
           % (ARRIVAL_T_SIGMA_MAX_S * 1e6, ARRIVAL_PATH_BIAS_MAX_S * 1e6,
              ARRIVAL_ONE_WAY_BUDGET_S * 1e6),
           "%-14s %-9s %10s %8s %11s %4s %5s  %-18s %s"
           % ("class", "time", "t_sigma", "range", "bias", "arr", "mics", "usable band", "notes")]
    for c in rows:
        lo, hi, lim = c.usable_band_hz()
        bias = "unmeasured" if c.path_bias_s is None else "%8.1f us" % (c.path_bias_s * 1e6,)
        out.append("%-14s %-9s %8.1f us %6.2f m %11s %4s %5d  %6.0f-%-6.0f %-3s %s"
                   % (c.name, c.time_source, c.t_sigma_s * 1e6, c.range_sigma_m(), bias,
                      "yes" if c.contributes_arrival() else "NO", c.mic_count,
                      lo, hi, lim[:3], c.notes))
    return "\n".join(out)


if __name__ == "__main__":   # pragma: no cover
    print(describe())
