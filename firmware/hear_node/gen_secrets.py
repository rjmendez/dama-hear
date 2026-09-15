#!/usr/bin/env python3
"""Emit firmware/hear_node/secrets.h from ~/.wifi. secrets.h is gitignored.

    python3 gen_secrets.py <node-id> [node-class]

The node id is REQUIRED and there is no default, because a default would be identical on every
node and two nodes that share an identity cannot be told apart in the record -- which makes every
row of a multi-node capture unusable for TDoA. Use a short lowercase name: it becomes the mDNS
hostname, the fallback AP SSID, the `node` column of every CSV, and the prefix of every clip file.

~/.wifi holds WIFI_<n>_SSID / WIFI_<n>_PSK pairs. Credentials are never printed -- this reports
only how many networks it found and a masked name, so a terminal log or a screenshot cannot leak
them.

~/.hear_push, if present, holds HEAR_PUSH_HOST=..., HEAR_PUSH_TOKEN=... and/or HEAR_ADMIN_TOKEN=...
to override the firmware's compiled-in heartbeat-push target/auth token and its admin-endpoint
token (Alert 1: /update, /reboot, /format, /gate, hardware sweeps). None of the three lines is
required -- a node built with no file here keeps the firmware's defaults, which for
HEAR_ADMIN_TOKEN means "" and therefore every privileged endpoint refuses every request.
"""
import os
import re
import sys


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


# HEAR_PUSH_HOST / HEAR_PUSH_TOKEN / HEAR_ADMIN_TOKEN: read from ~/.hear_push (KEY=value, one per
# line), same shape as ~/.wifi. None is required -- a node with no file here keeps the firmware's
# compiled-in defaults -- but a node that pushes heartbeats needs a token matching the
# hear-heartbeat-token k8s secret, and a node whose admin endpoints should answer at all needs its
# own HEAR_ADMIN_TOKEN. Neither value may ever be typed into a tracked file.
def read_push_config(path=None):
    path = path or os.path.expanduser("~/.hear_push")
    cfg = {}
    if not os.path.exists(path):
        return cfg
    line_re = re.compile(r"\s*(HEAR_PUSH_HOST|HEAR_PUSH_TOKEN|HEAR_ADMIN_TOKEN)\s*[:=]\s*(.*?)\s*$")
    with open(path) as f:
        for line in f:
            m = line_re.match(line)
            if m:
                cfg[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return cfg


def mask(s):
    return s[0] + "*" * max(len(s) - 2, 1) + s[-1] if len(s) > 2 else "**"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__.strip()); sys.exit(2)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--dir" in sys.argv: args = [a for a in args if a != sys.argv[sys.argv.index("--dir") + 1]]
    node_id = args[0].strip()
    node_class = args[1].strip() if len(args) > 1 else "xiao-s3-pps"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,22}", node_id):
        print("node id must be lowercase letters, digits and dashes, <=23 chars: %r" % node_id)
        sys.exit(2)

    src = os.path.expanduser("~/.wifi")
    # --dir lets one generator serve more than one sketch. puc_node needs its own secrets.h and
    # copying the file by hand is how a node ends up flashed with another node's identity.
    sketch = os.path.dirname(os.path.abspath(__file__))
    if "--dir" in sys.argv:
        sketch = os.path.abspath(sys.argv[sys.argv.index("--dir") + 1])
    out = os.path.join(sketch, "secrets.h")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from wifi_store import read_pairs  # noqa: E402

    pairs = read_pairs(src)
    if not pairs:
        print("no complete SSID/PSK pairs in %s" % src); sys.exit(1)

    with open(out, "w") as f:
        f.write("// GENERATED from ~/.wifi by gen_secrets.py -- gitignored, do not commit.\n")
        f.write("#pragma once\n#define WIFI_N %d\n" % len(pairs))
        f.write("static const char *WIFI_SSIDS[] = {%s};\n" % ", ".join('"%s"' % esc(s) for s, _ in pairs))
        f.write("static const char *WIFI_PASSES[] = {%s};\n" % ", ".join('"%s"' % esc(p) for _, p in pairs))
        f.write('#define NODE_ID "%s"\n' % esc(node_id))
        f.write('#define NODE_CLASS "%s"\n' % esc(node_class))
        push_cfg = read_push_config()
        if push_cfg.get("HEAR_PUSH_HOST"):
            f.write('#define HEAR_PUSH_HOST "%s"\n' % esc(push_cfg["HEAR_PUSH_HOST"]))
        if push_cfg.get("HEAR_PUSH_TOKEN"):
            f.write('#define HEAR_PUSH_TOKEN "%s"\n' % esc(push_cfg["HEAR_PUSH_TOKEN"]))
        if push_cfg.get("HEAR_ADMIN_TOKEN"):
            f.write('#define HEAR_ADMIN_TOKEN "%s"\n' % esc(push_cfg["HEAR_ADMIN_TOKEN"]))
        # FIRMWARE BUILD ID. Every node reported an identical /status shape while running binaries
        # built from different commits, and there was no field that could tell them apart -- so
        # "are all the nodes on the same version?" was not answerable from the fleet, only from
        # memory of who was flashed when. A capture whose nodes silently differ is not one capture.
        #
        # Recorded from git at GENERATION time, not build time, because secrets.h is what flash.py
        # regenerates per node and it is the only file guaranteed to be rewritten on every flash.
        # `-dirty` is not cosmetic here: it means the binary does not correspond to any commit, and
        # a node running one cannot be matched to a source tree afterwards.
        build = "unknown"
        try:
            import subprocess
            build = subprocess.run(["git", "describe", "--always", "--dirty", "--tags"],
                                   cwd=os.path.dirname(os.path.abspath(__file__)),
                                   capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
        except Exception:
            pass
        f.write('#define FW_BUILD "%s"\n' % esc(build))
    os.chmod(out, 0o600)

    print("wrote %s (0600) for node %r (class %r) with %d network(s): %s"
          % (out, node_id, node_class, len(pairs), ", ".join(mask(s) for s, _ in pairs)))
