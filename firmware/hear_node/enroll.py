#!/usr/bin/env python3
"""Enroll a hear node over USB: write a release image, then give the node its name and Wi-Fi.

    python3 firmware/hear_node/enroll.py rankine /dev/ttyACM0 --release v0.1.0
    python3 firmware/hear_node/enroll.py rankine /dev/ttyACM0 --input-dir <arduino-cli output dir>
    python3 firmware/hear_node/enroll.py rankine /dev/ttyACM0 --no-flash

The name and networks go into the node's NVS partition, which OTA never writes. After this, every
update is the matching public release image for that node's board class:

    python3 firmware/hear_node/flash.py <node> <ip> --release <tag>

Networks come from ~/.wifi and are never printed. Needs arduino-cli with the esp32 core (the same
toolchain flash.py uses) and pyserial.

The upload resets a board that is running this firmware into its bootloader over the USB serial
line. A board running anything else (a new one, or one that is wedged) has to be put there by hand:
hold BOOT while plugging it in. The board re-enumerates during the upload and on every reboot, so
under WSL2 attach it with `usbipd attach --wsl --busid <id> --auto-attach`, or the port vanishes.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import deploy_gate  # noqa: E402
import board_profiles  # noqa: E402
import release_manifest  # noqa: E402
import wifi_store  # noqa: E402

REPO_SLUG = "rjmendez/dama-hear"
FQBN = board_profiles.FQBN
SKETCH = "hear_node"
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,22}")
MAX_NETS = 8          # HEAR_PROV_MAX_NETS
LINE_MAX = 1024       # HEAR_PROV_LINE_MAX
CHUNK = 64            # bytes per USB write, well under one CDC packet


def die(msg, code=1):
    print("enroll: " + msg, file=sys.stderr)
    sys.exit(code)


def sign(line):
    return "%s crc=%08x" % (line, zlib.crc32(line.encode()) & 0xFFFFFFFF)


def prov_line(node, cls, pairs, ap_pass=None):
    """The signed PROV line hear_prov_line.h parses. Raises ValueError for anything it would refuse.

    ap_pass is the node's OWN fallback-AP password (Alert 5 -- see hear_prov_line.h's "ap" token).
    Optional: a caller that omits it gets a record with no ap_pass, and the firmware then derives
    one from its own MAC rather than falling back to a fleet-wide shared string.
    """
    if not ID_RE.fullmatch(node):
        raise ValueError("node id must be lowercase letters, digits and dashes: %r" % node)
    if cls and not ID_RE.fullmatch(cls):
        raise ValueError("bad class %r" % cls)
    if not pairs:
        raise ValueError("no networks")
    if len(pairs) > MAX_NETS:
        raise ValueError("%d networks; the node stores at most %d" % (len(pairs), MAX_NETS))
    toks = ["PROV", "v=1", "node=" + node] + (["class=" + cls] if cls else [])
    for i, (ssid, psk) in enumerate(pairs, 1):
        s, p = ssid.encode(), psk.encode()
        if not 1 <= len(s) <= 32 or not 8 <= len(p) <= 64 or b"\0" in s + p:
            raise ValueError("network %d is not a valid WPA2 SSID/passphrase pair" % i)
        toks.append("net=%s:%s" % (s.hex(), p.hex()))
    if ap_pass:
        a = ap_pass.encode()
        if not 8 <= len(a) <= 64 or b"\0" in a:
            raise ValueError("ap_pass must be 8-64 bytes with no embedded NUL")
        toks.append("ap=" + a.hex())
    line = sign(" ".join(toks))
    if len(line) >= LINE_MAX:
        raise ValueError("the PROV line is %d bytes; the node reads at most %d" % (len(line), LINE_MAX - 1))
    return line


def random_ap_pass(nbytes=12):
    """A per-device fallback-AP password with no operator input required -- see --ap-pass."""
    import secrets as _secrets
    return _secrets.token_hex(nbytes)  # 2*nbytes hex chars, well within the 8-64 char WPA2 range


def fetch(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def check_sums(sums_text, name, data):
    """Refuse an asset whose sha256 is missing from SHA256SUMS or does not match it."""
    want = None
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            want = parts[0].lower()
    if want is None:
        raise ValueError("%s is not listed in SHA256SUMS" % name)
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise ValueError("%s sha256 %s, SHA256SUMS says %s" % (name, got, want))


def release_files(tag, dest, board_class, repo=REPO_SLUG):
    board_profiles.require_board_class(board_class)
    base = "https://github.com/%s/releases/download/%s/" % (repo, tag)
    manifest_text = None
    try:
        manifest_text = fetch(base + release_manifest.MANIFEST_NAME).decode()
    except OSError:
        pass
    sums = None if manifest_text is not None else fetch(base + "SHA256SUMS").decode()
    fetched = {}
    for kind in ("app", "bootloader", "partitions"):
        name = board_profiles.release_asset_name(tag, board_class, kind)
        data = fetch(base + name)
        if manifest_text is None:
            check_sums(sums, name, data)
        fetched[name] = data
        with open(os.path.join(dest, board_profiles.upload_filename(kind)), "wb") as f:
            f.write(data)
    if manifest_text is not None:
        release_manifest.verify_downloaded_release_assets(manifest_text, tag, board_class, fetched)
        print("enroll: %s %s assets verified against %s"
              % (tag, board_class, release_manifest.MANIFEST_NAME))
    else:
        print("enroll: %s %s assets verified against SHA256SUMS (legacy release)"
              % (tag, board_class))


def upload(port, input_dir):
    sketch = os.path.join(REPO, "firmware", SKETCH)
    r = subprocess.run(["arduino-cli", "upload", "-p", port, "--fqbn", FQBN,
                        "--input-dir", input_dir, sketch])
    if r.returncode:
        die("upload failed. A board not running this firmware needs BOOT held while it is plugged "
            "in; under WSL2 the port must be attached with usbipd --auto-attach")


def open_port(port, wait_s=30):
    import serial
    t0 = time.time()
    while time.time() - t0 < wait_s:
        try:
            return serial.Serial(port, 115200, timeout=0.5)
        except (OSError, serial.SerialException):
            time.sleep(0.5)
    die("%s did not come back within %d s (under WSL2: usbipd attach --auto-attach)" % (port, wait_s))


def parse_state(line):
    """`PROV STATE src=nvs node=rankine nets=2 fw=v0.1.0 ip=172.16.100.50` -> dict, else None."""
    if not line.startswith("PROV STATE "):
        return None
    return dict(t.split("=", 1) for t in line.split()[2:] if "=" in t)


def ask_state(port, wait_s):
    """Poll PROV? until the node answers. The node reads serial only from loop(), i.e. after its
    Wi-Fi attempt in setup() has finished, so the answer already says whether it joined."""
    s = open_port(port)
    t0, last_q = time.time(), 0.0
    try:
        while time.time() - t0 < wait_s:
            if time.time() - last_q > 2:
                s.write(b"PROV?\n")
                last_q = time.time()
            st = parse_state(s.readline().decode("utf-8", "replace").strip())
            if st is not None:
                return st
    except OSError:
        pass              # the port vanished under a reset; the caller asks again
    finally:
        s.close()
    return None


def send_line(s, line):
    data = line.encode() + b"\n"
    for i in range(0, len(data), CHUNK):
        s.write(data[i:i + CHUNK])
        s.flush()
        time.sleep(0.02)


def exchange(port, line, wait_s=120):
    """Send the record, then wait for the node to come back on it and join Wi-Fi."""
    st = ask_state(port, wait_s)
    if st is None:
        die("the node never answered PROV? on %s -- is it running a release image?" % port)
    print("enroll: before  src=%s node=%s nets=%s fw=%s"
          % (st.get("src"), st.get("node"), st.get("nets"), st.get("fw")))
    s = open_port(port)
    send_line(s, line)
    t0 = time.time()
    while time.time() - t0 < 20:
        got = s.readline().decode("utf-8", "replace").strip()
        if got.startswith("PROV ERR"):
            die("the node refused the record: " + got)
        if got.startswith("PROV OK"):
            print("enroll: " + got)
            break
    else:
        die("no PROV OK within 20 s")
    s.close()
    t0 = time.time()
    while time.time() - t0 < wait_s:
        st = ask_state(port, wait_s - (time.time() - t0))
        if st and st.get("src") == "nvs":
            if st.get("ip", "none") == "none":
                die("%s rebooted on its NVS record but joined no network; it is its own access "
                    "point now" % st.get("node"))
            return st["ip"]
    die("the node did not come back on its NVS record within %d s" % wait_s)


def fetch_status(ip, timeout=6):
    with urllib.request.urlopen("http://%s/status" % ip, timeout=timeout) as r:
        return json.load(r)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("node")
    ap.add_argument("port", help="serial port, e.g. /dev/ttyACM0")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--release", help="release tag to download and verify, e.g. v0.1.0")
    src.add_argument("--input-dir", help="an arduino-cli output dir holding hear_node.ino.bin et al.")
    src.add_argument("--no-flash", action="store_true", help="the board already runs a release image")
    ap.add_argument("--class", dest="cls", default=board_profiles.DEFAULT_BOARD_CLASS,
                    choices=board_profiles.known_board_classes())
    ap.add_argument("--wifi", default=wifi_store.PATH)
    ap.add_argument("--allow-gps-no-fix-indoors", action="store_true",
                    help="accept selftest gps=no-fix after enrollment (for indoor bench work only)")
    ap.add_argument("--allow-pps-absent", action="store_true",
                    help="accept selftest pps=absent after enrollment")
    ap.add_argument("--ap-pass",
                    help="this node's own fallback-AP password (Alert 5: no shared default). "
                         "Omit to have one generated and printed for you to record.")
    ap.add_argument("--no-ap-pass", action="store_true",
                    help="provision no AP password at all; the node derives one from its own MAC "
                         "instead (unique per node, but not operator-chosen or secret-strength)")
    a = ap.parse_args(argv)

    ap_pass = None
    if not a.no_ap_pass:
        ap_pass = a.ap_pass or random_ap_pass()
        if not a.ap_pass:
            print("enroll: generated fallback-AP password for %s: %s (record this -- it is not "
                  "shown again)" % (a.node, ap_pass))

    try:
        pairs = wifi_store.read_pairs(a.wifi)
        line = prov_line(a.node, a.cls, pairs, ap_pass=ap_pass)
    except (OSError, ValueError) as e:
        die(str(e))
    print("enroll: %s (%s) with %d network(s): %s"
          % (a.node, a.cls, len(pairs), ", ".join(wifi_store.mask(s) for s, _ in pairs)))

    if a.release:
        d = os.path.join(REPO, ".otabuild", "release-%s-%s-usb" % (a.release, a.cls))
        os.makedirs(d, exist_ok=True)
        try:
            release_files(a.release, d, a.cls)
        except (OSError, ValueError) as e:
            die("release %s (%s): %s" % (a.release, a.cls, e))
        upload(a.port, d)
    elif a.input_dir:
        upload(a.port, a.input_dir)

    ip = exchange(a.port, line)
    print("enroll: %s joined Wi-Fi at %s" % (a.node, ip))
    try:
        st = fetch_status(ip, timeout=6)
    except OSError as e:
        die("%s joined Wi-Fi but is not answering live /status (%s)" % (ip, e))
    reasons = deploy_gate.status_reasons(
        st,
        node=a.node,
        allow_gps_no_fix_indoors=a.allow_gps_no_fix_indoors,
        allow_pps_absent=a.allow_pps_absent,
    )
    if reasons:
        die("%s failed the live readiness gate: %s" % (ip, "; ".join(reasons)))
    print("enroll: OK -- %s reports node=%r fw=%r prov=%r %s"
          % (ip, st["node"], st.get("fw"), st.get("prov"), deploy_gate.format_selftest(st)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
