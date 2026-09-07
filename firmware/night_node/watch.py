#!/usr/bin/env python3
"""Poll the night node and keep a durable record. Runs detached; survives this terminal.

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
            if prev["pps"]["edges"] == 0 and s["pps"]["edges"] > 0:
                say("*** FIRST PPS EDGE *** -- the sample-rate measurement has started")
            if s["pps"]["glitches"] > prev["pps"]["glitches"]:
                say("PPS glitches %d -> %d (noise on the wire, not a fast clock)"
                    % (prev["pps"]["glitches"], s["pps"]["glitches"]))
            if s["audio"]["detections"] != prev["audio"]["detections"]:
                say("detections %d -> %d" % (prev["audio"]["detections"], s["audio"]["detections"]))
            if s["uptime_s"] < prev["uptime_s"]:
                say("NODE REBOOTED (uptime went %ss -> %ss)" % (prev["uptime_s"], s["uptime_s"]))
            # the deliverable: report it once it is real, then only when it moves
            if s["pps"]["edges"] >= 3 and s["i2s"]["measured_hz"] > 0:
                a, b = prev["i2s"]["measured_hz"], s["i2s"]["measured_hz"]
                if a == 0 or abs(b - a) > 0.02:
                    say("I2S measured %.4f Hz (%+.1f ppm) from %d PPS edges"
                        % (b, s["i2s"]["ppm"], s["pps"]["edges"]))
        prev = s
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        misses += 1
        if up is not False and misses >= 2:
            say("node unreachable (%s)" % type(e).__name__)
            up = False
    except Exception as e:
        say("poll error: %r" % e)
    time.sleep(EVERY)
