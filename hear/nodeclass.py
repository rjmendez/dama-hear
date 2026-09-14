#!/usr/bin/env python3
"""What a node's hardware can and cannot contribute, as data the solvers can refuse on.

WHY THIS EXISTS. `hear/backend/survey.py` answers "where is node 7". Nothing answered "can node 7
produce an arrival time at all", and the honest answer is not always yes. A BirdWeather PUC has two
microphones, a full environmental suite and an on-device classifier, and it is a genuinely better
listener than the XIAO nodes -- but if its GPS PPS is not wired to a pin, its timestamps are NTP
timestamps, and an NTP timestamp in a TDoA solve is not a worse measurement, it is a different
measurement wearing the same units.

The number that makes this concrete: sound travels 343 mm in a millisecond. A node timed by PPS
carries tens of microseconds of timestamp error (nyquist measures tAcc 22-26 ns and a PPS interval
spread of 10-15 us over 11.33 h). A node timed by NTP carries units of milliseconds -- dama-gotchi's
own GPSTimingSync docstring claims 1-5 ms cross-device, which is 0.34-1.7 m. Averaging the two into
one solve does not split the difference; it takes the worse one and hides it behind a residual that
still looks fine, because a common-mode timing error moves every range together.

So capability is declared per class, checked at the door, and REFUSED rather than degraded --
the same discipline as SurveyError raising at load time instead of decorating every later answer.

⚠️A CLASS DESCRIBES HARDWARE, NOT A PROMISE. `xiao-s3-pps` says a node of that class HAS a PPS
input wired to D0. It does not say the GPS is talking: mach is that class and its module currently
decodes at no baud rate at all. Use the class to decide what a node could contribute and the
node's own telemetry to decide what it did.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

# Speed of sound at 20 C, only used to turn a timing error into a distance for the messages below.
# The live value comes from a node's own BMP280 (hear reports env.c_mps); this is for exposition.
_C_NOMINAL_MPS = 343.0


class CapabilityError(ValueError):
    """A node was asked for something its hardware cannot do. ValueError subclass so a caller that
    already catches the repo's refusals catches this too."""


class NodeClass:
    """One hardware configuration.

    Fields are deliberately about MEASURABLE hardware limits, not about intent:

    time_source     "gps_pps" | "ntp" | "none"
                    The only field that decides TDoA eligibility. "gps_pps" means a PPS edge
                    reaches a GPIO and the firmware disciplines its clock to it.
    t_sigma_s       Timestamp uncertainty, seconds, 1-sigma. This is the number a solver should
                    propagate, and it is what makes classes non-interchangeable.
    mic_count       2 or more permits an on-node bearing from the fixed baseline; 1 does not.
    fs_hz           Sample rate. Nyquist is fs/2 and no filter, model or integration time
                    recovers anything above it.
    band_hz         (lo, hi) the MICROPHONE passes, before Nyquist is applied. The tighter of
                    this and Nyquist is what the node can actually hear -- see usable_band_hz().
    env             Sensors present, as a set of names. "temp" is the one that matters: sound
                    speed moves 0.6 m/s per degree and biases every node the same way, so it
                    does NOT cancel in TDoA.
    raw_retain_s    Seconds of raw audio retrievable on request, 0 if none.
    power_profile   "solar_duty_cycled" | "mains_continuous" | "battery_mobile". A SECOND
                    hardware fact, orthogonal to time_source: it says nothing about timing or
                    ranging and everything about which DETECTOR LANE this class can run. A
                    solar node's energy budget rules out running an FFT-based detector
                    continuously; a mains node's does not. See docs/node-profiles.md, which is
                    the design this field exists to make checkable rather than asserted per node.
    has_camera      Whether an image sensor is fitted. Decides eligibility for the future
                    webcam/wildlife-camera profile's multimodal event fusion -- see
                    docs/node-profiles.md section on that profile. False for every class today;
                    no such node has been built.
    notes           Free text for whoever reads a refusal message.
    """

    def __init__(self, name: str, time_source: str, t_sigma_s: float, mic_count: int,
                 fs_hz: float, band_hz: Sequence[float], env: Sequence[str] = (),
                 raw_retain_s: float = 0.0, power_profile: str = "mains_continuous",
                 has_camera: bool = False, notes: str = "") -> None:
        if not isinstance(name, str) or not name.strip():
            raise CapabilityError("class name must be a non-empty string")
        if time_source not in ("gps_pps", "ntp", "none"):
            raise CapabilityError("unknown time_source %r" % (time_source,))
        if power_profile not in ("solar_duty_cycled", "mains_continuous", "battery_mobile"):
            raise CapabilityError("unknown power_profile %r" % (power_profile,))
        if isinstance(mic_count, bool) or not isinstance(mic_count, int) or mic_count < 1:
            raise CapabilityError("%s: mic_count must be >= 1" % name)
        try:
            t_sigma_s = float(t_sigma_s)
            fs_hz = float(fs_hz)
            raw_retain_s = float(raw_retain_s)
            lo, hi = (float(v) for v in band_hz)
        except (TypeError, ValueError, IndexError):
            raise CapabilityError("%s: numeric fields are malformed" % name)
        if not math.isfinite(fs_hz) or fs_hz <= 0:
            raise CapabilityError("%s: fs_hz must be positive" % name)
        if not math.isfinite(lo) or not math.isfinite(hi) or lo < 0 or not lo < hi:
            raise CapabilityError("%s: band_hz must be (lo, hi) with lo < hi" % name)
        if not math.isfinite(t_sigma_s) or t_sigma_s <= 0:
            raise CapabilityError("%s: t_sigma_s must be positive -- a node with no timing error "
                                  "is a claim no hardware supports" % name)
        if not math.isfinite(raw_retain_s) or raw_retain_s < 0:
            raise CapabilityError("%s: raw_retain_s must be finite and non-negative" % name)
        self.name = name
        self.time_source = time_source
        self.power_profile = power_profile
        self.has_camera = bool(has_camera)
        self.t_sigma_s = t_sigma_s
        self.mic_count = mic_count
        self.fs_hz = fs_hz
        self.band_hz = (lo, hi)
        self.env = frozenset(env)
        self.raw_retain_s = raw_retain_s
        self.notes = notes

    # ---- what follows from the fields ------------------------------------------------------
    @property
    def nyquist_hz(self) -> float:
        return self.fs_hz / 2.0

    def usable_band_hz(self):
        """(lo, hi, limit) -- the band this node can actually deliver, and WHICH ceiling bound it.

        `limit` is "microphone" or "nyquist", and they need different remedies: a Nyquist ceiling
        is fixed by sampling faster, a microphone ceiling is fixed only by a different part. The
        distinction is not pedantic -- the XIAO nodes are Nyquist-bound at 8 kHz today and would
        still be microphone-bound at 15 kHz with the planned ICS-43434.
        """
        hi, limit = self.band_hz[1], "microphone"
        if self.nyquist_hz < hi:
            hi, limit = self.nyquist_hz, "nyquist"
        return self.band_hz[0], hi, limit

    def range_sigma_m(self, c_mps: float = _C_NOMINAL_MPS) -> float:
        """The timestamp error expressed as distance. This is the honest cost of a class."""
        c_mps = float(c_mps)
        if not math.isfinite(c_mps) or c_mps <= 0:
            raise CapabilityError("sound speed must be finite and positive")
        return self.t_sigma_s * c_mps

    def can_hear(self, f_lo_hz: float, f_hi_hz: float) -> bool:
        """Does the requested band lie wholly inside what this node delivers?"""
        f_lo_hz, f_hi_hz = float(f_lo_hz), float(f_hi_hz)
        if not math.isfinite(f_lo_hz) or not math.isfinite(f_hi_hz) or f_lo_hz < 0 or not f_lo_hz < f_hi_hz:
            return False
        lo, hi, _ = self.usable_band_hz()
        return f_lo_hz >= lo and f_hi_hz <= hi

    def can_bear(self) -> bool:
        """Can this node produce a bearing on its own? Needs at least two microphones."""
        return self.mic_count >= 2

    def contributes_arrival(self) -> bool:
        """Is this node's timestamp admissible as a TDoA arrival at all?"""
        return self.time_source == "gps_pps"

    def detector_lanes(self) -> frozenset:
        """Which detector lanes this class's POWER budget admits, as a set of names.

        This is a capability check, the same discipline as contributes_arrival() -- a lane a
        class cannot afford is refused, not silently degraded. It says nothing about whether a
        lane is currently WIRED (that is firmware state, exactly the distinction `can_hear`'s
        docstring already draws between what a class HAS and what it is DOING); it says only
        what a caller could ever ask this power budget to run.

            "impulse_gate"   hear/node/detect.py's broadband amplitude gate. A 1 ms boxcar and a
                              comparison, cheap enough for every profile including a duty-cycled
                              solar node -- see docs/node-profiles.md.
            "tonal_gate_duty" modules/bioacoustic/detect.py's TonalGate, run only during a
                              node's already-awake windows rather than continuously. Available to
                              every profile that has a microphone at all; it is what a solar node
                              gets instead of a continuous tonal lane.
            "tonal_gate_continuous" the same detector, run without duty-cycling. Needs a power
                              budget that does not have to sleep to make its energy budget --
                              mains and (for as long as the phone stays plugged in or awake)
                              battery-mobile profiles only.
            "camera_fusion"   correlating an image trigger with an audio burst tag. Needs
                              has_camera; no class has it yet.

        docs/node-profiles.md is the design; this method is what stops that design from
        drifting out of sync with what NodeClass actually declares.
        """
        lanes = {"impulse_gate", "tonal_gate_duty"}
        if self.power_profile in ("mains_continuous", "battery_mobile"):
            lanes.add("tonal_gate_continuous")
        if self.has_camera:
            lanes.add("camera_fusion")
        return frozenset(lanes)

    def __repr__(self) -> str:
        lo, hi, lim = self.usable_band_hz()
        return ("NodeClass(%s, %s, t_sigma=%.0f us = %.2f m, %d mic, %.0f-%.0f Hz [%s])"
                % (self.name, self.time_source, self.t_sigma_s * 1e6,
                   self.range_sigma_m(), self.mic_count, lo, hi, lim))


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
    # spread 10-15 us, zero glitches in 40825 edges. The dominant term is NOT the GPS -- it is the
    # 256-sample I2S block, which the firmware back-dates but only to about one sample. 100 us is
    # a deliberately conservative round number above the parts that have been measured; it has
    # never been checked against an external reference because the node has no second clock.
    #
    # ⚠️THE BUDGET HOLDS ON THE DETECTION PATH AND USED TO FAIL BY 11x ON THE RAW-RING PATH.
    # Termwise RSS for a dets.csv arrival, 2026-09-08: GPS tAcc 0.02 us, PPS spread/2 5.00 us,
    # esp_timer between anchors 12.20 us (9.5-12.2 ppm vs GPS, re-zeroed every PPS second),
    # I2S block-quantisation residual ~1 sample 62.47 us, fs_clean back-date differential
    # 8.90 us -> 64.47 us = 22.1 mm. It fits, and the term that dominates it is the one this
    # comment already named.
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
    mic_count=1,
    fs_hz=16000.0,
    # The XIAO's onboard PDM MEMS part. The low edge is where the firmware's DC block sits (1.6 Hz)
    # and the high edge is well above Nyquist, so Nyquist is what binds -- usable_band_hz() says so.
    band_hz=(50.0, 10000.0),
    env=("temp", "press"),
    raw_retain_s=240.0,
    # Tier-1 field design target is solar + LoRa (docs/node-hardware.md: "You supply the
    # MCU, LoRa, battery and solar"). nyquist/mach are currently bench-run on a mains plug
    # timer for validation, which is a TEST HARNESS choice, not the class this hardware was
    # designed to. See docs/node-profiles.md for the duty-cycled detector lane this implies.
    power_profile="solar_duty_cycled",
    notes="XIAO ESP32-S3 Sense + u-blox GPS on D0 PPS + BMP280 + microSD. nyquist, mach.",
))

register(NodeClass(
    name="esp32s3-i2s-gps",
    time_source="gps_pps",
    t_sigma_s=100e-6,
    mic_count=1,
    fs_hz=48000.0,
    band_hz=(50.0, 15000.0),
    env=("temp", "press"),
    raw_retain_s=80.0,
    power_profile="solar_duty_cycled",
    notes="ESP32-S3-WROOM + Adafruit Ultimate GPS v3 (PPS GPIO4, UART 15/16) + ICS-43434 I2S mic (GPIO 41/42/1) + RGB GPIO48. Gold, Kasami, Ageev.",
))


register(NodeClass(
    name="xiao-s3-i2s",
    time_source="gps_pps",
    t_sigma_s=100e-6,
    mic_count=1,
    fs_hz=48000.0,
    # ICS-43434, docs/node-hardware.md "What the ICS-43434 can and cannot do": 50 Hz - 15 kHz,
    # low-passed above 24 kHz, NO ultrasonic content at any sample rate. At 48 kHz the microphone
    # binds rather than Nyquist, which is the whole reason that section exists.
    band_hz=(50.0, 15000.0),
    env=("temp", "press"),
    raw_retain_s=80.0,   # same PSRAM budget, three times the rate
    power_profile="solar_duty_cycled",
    notes="Planned I2S variant. Not built. Bandwidth-limited by the part, not by the sample rate.",
))

register(NodeClass(
    name="puc-pps",
    time_source="gps_pps",
    # PROVISIONAL. Same order as the XIAO nodes IF the GPS PPS reaches a GPIO -- unverified, and
    # the single fact that decides whether this class exists at all. See docs/node-classes.md.
    t_sigma_s=100e-6,
    mic_count=2,
    fs_hz=48000.0,
    band_hz=(50.0, 15000.0),
    env=("temp", "humidity", "press", "voc", "co2", "light"),
    raw_retain_s=0.0,
    # BirdWeather PUCs are USB/mains powered, WiFi-connected weather-station-style units --
    # no battery budget to protect, hence no duty-cycled lane. See docs/node-profiles.md.
    power_profile="mains_continuous",
    notes="BirdWeather PUC, IF its GPS PPS is wired to the ESP32-S3. UNVERIFIED -- do not survey a "
          "PUC as this class until a scope or a teardown confirms the PPS pin.",
))

register(NodeClass(
    name="puc-ntp",
    time_source="ntp",
    # dama-gotchi's GPSTimingSync docstring claims 1-5 ms cross-device sync for the same class of
    # (UTC, monotonic) anchoring. 3 ms is the middle of that, and it is 1.0 m of range. This class
    # is admissible for classification and environment and is REFUSED for arrivals.
    t_sigma_s=3e-3,
    mic_count=2,
    fs_hz=48000.0,
    band_hz=(50.0, 15000.0),
    env=("temp", "humidity", "press", "voc", "co2", "light"),
    raw_retain_s=0.0,
    power_profile="mains_continuous",
    notes="BirdWeather PUC timed by NTP and its RTC. An RTC gives holdover, not sync. Excellent "
          "listener, not a ranging node.",
))


register(NodeClass(
    name="gotchi-phone",
    time_source="none",
    # NOT "ntp". The phone HAS a good clock -- GPSTimingSync anchors (UTC, CLOCK_BOOTTIME) at each
    # GPS fix and claims 1-5 ms, which would be a usable NTP-class node. The problem is upstream of
    # the clock: the audio path never asks it what time it is.
    #
    # AcousticAntCollector.java:240 takes System.nanoTime() AFTER record.read() returns and calls
    # that the event time. Read as an arrival, that number carries:
    #
    #   hop quantisation        HOP_SIZE 1024 @ 44100 Hz = 23.2 ms, one stamp for the whole chunk
    #                           -> 6.7 ms sd and an 11.6 ms bias, before anything else
    #   AudioRecord buffering   max(minBuf, 8192 B) = 4096 frames = 92.9 ms of slack
    #   Android input HAL       20-100 ms, device-specific, NEVER MEASURED on this fleet
    #   ---------------------------------------------------------------------------------
    #   about 25 ms of scatter on about 70 ms of per-device BIAS = 8.6 m on 24.6 m
    #
    # The bias is the disqualifying part. Scatter averages down over events; a constant per-device
    # offset does not, and in TDoA it moves that node's range by 24.6 m -- half again the entire
    # nyquist-mach baseline. It is also invisible: it shifts the answer without touching the
    # residual in a 3-node exactly-determined fit.
    #
    # Two further facts, both from the source:
    #   - AudioRecord.getTimestamp() is never called anywhere in the app. GPSTimingSync's own
    #     docstring names it as the intended input and nothing uses it.
    #   - The impulse callback DISCARDS the timestamp it is handed:
    #     DeviceMetricsPoller.java:869 is `(peakDb, riseDb, ns) -> takAcou.onAcousticImpulse(
    #     peakDb, riseDb)`. There is no arrival time leaving the phone today at all.
    #   - No raw PCM is retained, so cross-correlation against a node is not possible either --
    #     only onsets, and onsets are what the timestamp problem ruins.
    t_sigma_s=25.1e-3,
    mic_count=1,
    fs_hz=44100.0,
    band_hz=(50.0, 20000.0),
    env=("temp", "press"),
    raw_retain_s=0.0,
    # Charged intermittently, carried, screen/CPU shared with the rest of the phone -- not a
    # duty-cycled sensor and not guaranteed always-on either. Own bucket rather than forced
    # into "solar_duty_cycled" (it has no solar budget logic at all) or "mains_continuous"
    # (it is not plugged in). See docs/node-profiles.md.
    power_profile="battery_mobile",
    notes="dama-gotchi Android node. EXCELLENT sensor platform and a good clock; the audio path "
          "does not use either. Refused for arrivals until AcousticAntCollector stamps frames "
          "with AudioRecord.getTimestamp() converted through GPSTimingSync.toUtcMs(), and the "
          "impulse callback stops dropping the timestamp. Fixed, it would be ~2.8 ms (0.98 m).",
))


register(NodeClass(
    name="hugbot-corroborator",
    time_source="none",
    t_sigma_s=100e-3,
    mic_count=1,
    fs_hz=44100.0,
    band_hz=(50.0, 15000.0),
    env=("temp", "press"),
    raw_retain_s=0.0,
    power_profile="battery_mobile",
    notes="Hugbot mobile acoustic corroborator. External ground-truth / presence corroborator only. Refused for TDoA arrival solving.",
))


register(NodeClass(
    name="esp32s3-cam-mains",
    # UNBUILT. Every field below is a design target for docs/node-profiles.md's future
    # webcam/wildlife-camera profile, not a measurement -- no such node exists yet, and this
    # entry exists so a caller can be written and tested against the capability model before the
    # hardware does. Do NOT survey a real node as this class; see puc-pps's identical caveat.
    time_source="gps_pps",
    # Same order as xiao-s3-pps IF a PPS line is wired; camera and TDoA timing are independent
    # design choices and nothing here couples them. A build with no PPS wired is "none", not this.
    t_sigma_s=100e-6,
    mic_count=1,
    fs_hz=16000.0,
    band_hz=(50.0, 10000.0),
    env=("temp", "press"),
    raw_retain_s=3600.0,     # mains power and no LoRa payload cap: retention is an SD budget only
    power_profile="mains_continuous",
    has_camera=True,
    notes="DESIGN TARGET, NOT BUILT. XIAO ESP32-S3 Sense in a mains-powered wildlife-camera "
          "role: continuous audio (impulse_gate + tonal_gate_continuous) plus onboard OV2640 "
          "motion/presence trigger, correlated by shared timestamp into one multimodal event. "
          "See docs/node-profiles.md.",
))


def get(name: str) -> NodeClass:
    try:
        return CLASSES[name]
    except KeyError:
        raise CapabilityError("unknown node class %r (have %s)"
                              % (name, ", ".join(sorted(CLASSES))))


# ---------------------------------------------------------------- the door
def require_arrival(name: str, node_id=None) -> NodeClass:
    """Admit a class as a source of TDoA arrivals, or raise saying what it would have cost.

    This is the function that makes the model worth having. A caller that skips it and averages an
    NTP node into a PPS solve gets an answer with a plausible residual and a metre of common-mode
    error, which no later check can find.
    """
    cls = get(name)
    if cls.contributes_arrival():
        return cls
    where = "" if node_id is None else " (node %r)" % (node_id,)
    raise CapabilityError(
        "class %r%s is timed by %s, not by GPS PPS, so its timestamps are not TDoA arrivals: "
        "t_sigma %.1f ms is %.2f m of range against a %s budget. Use it for classification, "
        "bearing and environment instead."
        % (cls.name, where, cls.time_source, cls.t_sigma_s * 1e3, cls.range_sigma_m(),
           "183 us" ))


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
    """Per-class range error for a set of classes, so a mixed network's weakest link is visible
    before it is averaged into an answer rather than after."""
    return {n: get(n).range_sigma_m(c_mps) for n in dict.fromkeys(names)}


def describe(names: Optional[Sequence[str]] = None) -> str:
    """A table for a human. Kept here so the refusal messages and the documentation cannot drift."""
    rows = [get(n) for n in (names if names is not None else sorted(CLASSES))]
    out = ["%-14s %-9s %10s %8s %5s  %-18s %s"
           % ("class", "time", "t_sigma", "range", "mics", "usable band", "notes")]
    for c in rows:
        lo, hi, lim = c.usable_band_hz()
        out.append("%-14s %-9s %8.1f us %6.2f m %5d  %6.0f-%-6.0f %-3s %s"
                   % (c.name, c.time_source, c.t_sigma_s * 1e6, c.range_sigma_m(), c.mic_count,
                      lo, hi, lim[:3], c.notes))
    return "\n".join(out)


if __name__ == "__main__":   # pragma: no cover
    print(describe())
