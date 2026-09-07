#!/usr/bin/env python3
"""Emit firmware/night_node/secrets.h from ~/.wifi. secrets.h is gitignored.

~/.wifi holds WIFI_<n>_SSID / WIFI_<n>_PSK pairs. Credentials are never printed -- this reports
only how many networks it found and a masked name, so a terminal log or a screenshot cannot leak
them.
"""
import os
import re
import sys

src = os.path.expanduser("~/.wifi")
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.h")

nets = {}
for line in open(src):
    m = re.match(r"\s*WIFI_(\d+)_(SSID|PSK)\s*[:=]\s*(.*?)\s*$", line)
    if m:
        nets.setdefault(m.group(1), {})[m.group(2)] = m.group(3).strip().strip('"').strip("'")

pairs = [(v["SSID"], v["PSK"]) for _, v in sorted(nets.items(), key=lambda kv: int(kv[0]))
         if v.get("SSID") and v.get("PSK")]
if not pairs:
    print("no complete SSID/PSK pairs in %s" % src); sys.exit(1)


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


with open(out, "w") as f:
    f.write("// GENERATED from ~/.wifi by gen_secrets.py -- gitignored, do not commit.\n")
    f.write("#pragma once\n#define WIFI_N %d\n" % len(pairs))
    f.write("static const char *WIFI_SSIDS[] = {%s};\n" % ", ".join('"%s"' % esc(s) for s, _ in pairs))
    f.write("static const char *WIFI_PASSES[] = {%s};\n" % ", ".join('"%s"' % esc(p) for _, p in pairs))
os.chmod(out, 0o600)

def mask(s):
    return s[0] + "*" * max(len(s) - 2, 1) + s[-1] if len(s) > 2 else "**"

print("wrote %s (0600) with %d network(s): %s" % (out, len(pairs), ", ".join(mask(s) for s, _ in pairs)))
