#!/usr/bin/env python3
"""Poll the node and keep a durable record. Runs detached; survives this terminal.

    nohup python3 watch.py http://damahear.local >> ~/dama-hear-night.log 2>&1 &

Writes one JSON object per poll to ~/dama-hear-night.jsonl, and prints a line ONLY when something
changes -- fix gained or lost, the first PPS edge, glitches climbing, the node going away or coming
back. A log that prints every poll is a log nobody reads in the morning.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://damahear.local").rstrip("/")
EVERY = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
JSONL = os.path.expanduser("~/dama-hear-night.jsonl")


def say(msg):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def poll():
    with urllib.request.urlopen(BASE + "/status", timeout=8) as r:
        return json.load(r)


pps_announced = False      # reset on a detected reboot, below
say("watching %s every %.0fs -> %s" % (BASE, EVERY, JSONL))
prev, up, misses = None, None, 0
while True:
    try:
        s = poll()
        s["wall"] = time.time()
        with open(JSONL, "a") as f:
            f.write(json.dumps(s) + "\n")

        if up is False or up is None:
            say("node reachable: uptime %ss, sd=%s" % (s["uptime_s"], s["sd"]))
        up, misses = True, 0

        if prev:
            if s["gps"]["fix"] != prev["gps"]["fix"]:
                say("GPS fix %d -> %d (%d sats, %s UTC)" % (prev["gps"]["fix"], s["gps"]["fix"],
                                                            s["gps"]["sats"], s["gps"]["utc"]))
            # Announce the first edge once per BOOT. Comparing only against the previous poll
            # made every reflash a fresh "first", so the log said something untrue twice while
            # PPS was in fact still reading zero.
            if s["uptime_s"] < prev["uptime_s"]:
                pps_announced = False          # new boot: the next edge is genuinely a first
            if s["pps"]["edges"] > 0 and not pps_announced:
                pps_announced = True
                say("*** FIRST PPS EDGE *** -- the sample-rate measurement has started")
            if s["pps"]["glitches"] > prev["pps"]["glitches"]:
                say("PPS glitches %d -> %d (noise on the wire, not a fast clock)"
                    % (prev["pps"]["glitches"], s["pps"]["glitches"]))
            a, pa = s.get("audio", {}), prev.get("audio", {})
            if a.get("detections") != pa.get("detections") and a.get("detections") is not None:
                say("detections %s -> %s" % (pa.get("detections"), a.get("detections")))
            t_, pt = s.get("time", {}), prev.get("time", {})
            if t_.get("valid") != pt.get("valid"):
                say("UTC anchor %s" % ("ACQUIRED" if t_.get("valid") else "LOST"))
            if t_.get("label_rejects", 0) > pt.get("label_rejects", 0):
                say("*** SECOND-LABEL REJECTED *** %s -> %s -- a 1 s mislabel is 343 m; it was caught"
                    % (pt.get("label_rejects"), t_.get("label_rejects")))
            if s["uptime_s"] < prev["uptime_s"]:
                say("NODE REBOOTED (uptime went %ss -> %ss)" % (prev["uptime_s"], s["uptime_s"]))
            # the deliverable: report it once it is real, then only when it moves
            # ⚠️SUPPORT, NOT EDGE COUNT. This used to announce the figure "from N PPS edges",
            # which is every edge the node ever saw -- including the ones on either side of a
            # stall. The rate is now averaged only over seconds the node certified as neither
            # short nor long, and `clean_s` is that population; announcing anything else here
            # would put a window behind a number that was not measured over it.
            if s["i2s"].get("clean_s", 0) >= 8 and s["i2s"]["measured_hz"] > 0:
                a, b = prev["i2s"]["measured_hz"], s["i2s"]["measured_hz"]
                if a == 0 or abs(b - a) > 0.02:
                    say("I2S measured %.4f Hz (%+.1f ppm) over %d clean GPS seconds"
                        % (b, s["i2s"]["ppm"], s["i2s"]["clean_s"]))
        prev = s
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        misses += 1
        if up is not False and misses >= 2:
            say("node unreachable (%s)" % type(e).__name__)
            up = False
    except Exception as e:
        say("poll error: %r" % e)
    time.sleep(EVERY)
