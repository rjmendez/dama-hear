#!/usr/bin/env python3
"""Build and flash ONE node, with its identity and its destination named in the same command.

    python3 firmware/hear_node/flash.py mach 172.16.100.116     # over the air
    python3 firmware/hear_node/flash.py mach /dev/ttyACM0       # over USB
    python3 firmware/hear_node/flash.py nyquist nyquist.local

WHY THIS EXISTS. Generating secrets.h and choosing where to send the image were two separate
manual steps, and I got them out of order: regenerated secrets.h for `nyquist`, then flashed that
build to `mach`. For two boots mach wrote rows labelled `nyquist` into its own health.csv,
dets.csv and scene.csv, and `mach.local` stopped resolving because two nodes were advertising one
mDNS name. That is exactly the failure the node-id work was added to prevent, committed and then
reproduced within the hour, because the safeguard lived in the data format and not in the act of
flashing.

So the two steps are one step. The identity cannot be stale relative to the target because the
target is not chosen separately from it.

AFTER FLASHING it reads /status back and REFUSES to report success if the node that answers is not
the node that was asked for. A flash that silently lands the wrong identity is the failure being
fixed; catching it needs the check to be part of the same command too.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SKETCH = os.path.relpath(HERE, REPO)
FQBN = "esp32:esp32:XIAO_ESP32S3:PSRAM=opi"


def die(msg, code=1):
    print("flash: " + msg, file=sys.stderr)
    sys.exit(code)


def status(host, timeout=6):
    url = host if host.startswith("http") else "http://%s" % host
    with urllib.request.urlopen(url + "/status", timeout=timeout) as r:
        return json.load(r)


def main(argv):
    if len(argv) < 3:
        print(__doc__.strip())
        return 2
    node, target = argv[1].strip(), argv[2].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,22}", node):
        die("node id must be lowercase letters, digits and dashes: %r" % node)

    is_serial = target.startswith("/dev/")
    outdir = os.path.join(REPO, ".otabuild", node)

    # 1. identity, for THIS node, immediately before the build that carries it
    subprocess.run([sys.executable, os.path.join(HERE, "gen_secrets.py"), node],
                   cwd=REPO, check=True)

    # 2. if the target is already reachable, refuse a target that is a DIFFERENT node. Flashing a
    #    node with someone else's identity is recoverable; doing it without noticing is not.
    if not is_serial:
        try:
            was = status(target)
            if was.get("node") not in (node, None):
                die("%s currently reports node=%r, not %r. Refusing: name the right target, or "
                    "pass --force if you really are re-identifying this board."
                    % (target, was.get("node"), node)
                    if "--force" not in argv else "")
        except Exception:
            pass          # not up yet, or first flash. Not a reason to refuse.

    # 3. build
    print("flash: building %s for %s" % (node, target))
    # --libraries: the shared platform code lives in firmware/lib/hear_platform and arduino-cli
    # will not find it otherwise. Without this the build fails on the first symbol that moved,
    # which is a confusing way to learn that a library exists.
    r = subprocess.run(["arduino-cli", "compile", "--fqbn", FQBN,
                        "--libraries", os.path.join(REPO, "firmware", "lib"),
                        "--output-dir", outdir, SKETCH], cwd=REPO)
    if r.returncode:
        die("compile failed")

    bin_path = os.path.join(outdir, "hear_node.ino.bin")
    if not os.path.exists(bin_path):
        die("no image at %s" % bin_path)

    # 4. flash
    if is_serial:
        r = subprocess.run(["arduino-cli", "upload", "-p", target, "--fqbn", FQBN,
                            "--input-dir", outdir, SKETCH], cwd=REPO)
        if r.returncode:
            die("upload failed")
    else:
        r = subprocess.run(["curl", "-s", "-m", "120", "-F", "firmware=@" + bin_path,
                            "http://%s/update" % target], capture_output=True, text=True)
        print("flash: %s" % r.stdout.strip())
        if "OK" not in r.stdout:
            die("OTA rejected: %s%s" % (r.stdout.strip(), r.stderr.strip()))

    # 5. verify the node that comes back is the node that was asked for
    host = target if not is_serial else None
    if host is None:
        print("flash: serial upload done; verify with  curl http://%s.local/status" % node)
        return 0
    for _ in range(40):
        time.sleep(3)
        try:
            now = status(host)
        except Exception:
            continue
        got = now.get("node")
        if got == node:
            print("flash: OK -- %s reports node=%r class=%r uptime=%ss"
                  % (host, got, now.get("class"), now.get("uptime_s")))
            return 0
        die("FLASHED THE WRONG IDENTITY: %s now reports node=%r, expected %r" % (host, got, node))
    die("node did not come back within 120 s -- check it before flashing anything else")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
