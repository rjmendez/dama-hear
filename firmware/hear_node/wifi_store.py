"""~/.wifi -> [(ssid, psk), ...], for gen_secrets.py and enroll.py alike, so the two cannot read
the same file differently. The file holds WIFI_<n>_SSID / WIFI_<n>_PSK pairs."""
import os
import re

PATH = os.path.expanduser("~/.wifi")
_LINE = re.compile(r"\s*WIFI_(\d+)_(SSID|PSK)\s*[:=]\s*(.*?)\s*$")


def read_pairs(path=PATH):
    nets = {}
    with open(path) as f:
        for line in f:
            m = _LINE.match(line)
            if m:
                nets.setdefault(m.group(1), {})[m.group(2)] = m.group(3).strip().strip('"').strip("'")
    return [(v["SSID"], v["PSK"]) for _, v in sorted(nets.items(), key=lambda kv: int(kv[0]))
            if v.get("SSID") and v.get("PSK")]


def mask(s):
    return s[0] + "*" * max(len(s) - 2, 1) + s[-1] if len(s) > 2 else "**"
