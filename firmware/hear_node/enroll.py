#!/usr/bin/env python3
"""Enroll a hear node over USB: write a release image, then give the node its name and Wi-Fi.

    python3 firmware/hear_node/enroll.py rankine /dev/ttyACM0 --release v0.1.0
    python3 firmware/hear_node/enroll.py rankine /dev/ttyACM0 --input-dir <arduino-cli output dir>
    python3 firmware/hear_node/enroll.py rankine /dev/ttyACM0 --no-flash

The name and networks go into the node's NVS partition, which OTA never writes. After this, every
update is the same public release image for every node:

    python3 firmware/hear_node/flash.py <node> <ip> --release <tag>

Networks come from ~/.wifi and are never printed. Needs arduino-cli with the esp32 core (the same
toolchain flash.py uses) and pyserial. If the upload cannot reach a brand-new board, hold BOOT
while plugging it in and run again.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import wifi_store  # noqa: E402

REPO_SLUG = "rjmendez/dama-hear"
FQBN = "esp32:esp32:XIAO_ESP32S3:PSRAM=opi"
SKETCH = "hear_node"
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,22}")
MAX_NETS = 8          # HEAR_PROV_MAX_NETS
LINE_MAX = 1024       # HEAR_PROV_LINE_MAX
# release asset -> the name arduino-cli upload --input-dir expects
UPLOAD_FILES = {"hear_node-{tag}.bin": SKETCH + ".ino.bin",
                "hear_node-{tag}-bootloader.bin": SKETCH + ".ino.bootloader.bin",
                "hear_node-{tag}-partitions.bin": SKETCH + ".ino.partitions.bin"}


def die(msg, code=1):
    print("enroll: " + msg, file=sys.stderr)
    sys.exit(code)


def prov_line(node, cls, pairs):
    """The PROV line hear_prov_line.h parses. Raises ValueError for anything it would refuse."""
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
    line = " ".join(toks)
    if len(line) >= LINE_MAX:
        raise ValueError("the PROV line is %d bytes; the node reads at most %d" % (len(line), LINE_MAX - 1))
    return line


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


def release_files(tag, dest, repo=REPO_SLUG):
    base = "https://github.com/%s/releases/download/%s/" % (repo, tag)
    sums = fetch(base + "SHA256SUMS").decode()
    for asset, local in UPLOAD_FILES.items():
        name = asset.format(tag=tag)
        data = fetch(base + name)
        check_sums(sums, name, data)
        with open(os.path.join(dest, local), "wb") as f:
            f.write(data)
    print("enroll: %s assets verified against SHA256SUMS" % tag)


def upload(port, input_dir):
    sketch = os.path.join(REPO, "firmware", SKETCH)
    r = subprocess.run(["arduino-cli", "upload", "-p", port, "--fqbn", FQBN,
                        "--input-dir", input_dir, sketch])
    if r.returncode:
        die("upload failed (for a brand-new board, hold BOOT while plugging it in and retry)")


def open_port(port, wait_s=30):
    import serial
    t0 = time.time()
    while time.time() - t0 < wait_s:
        try:
            return serial.Serial(port, 115200, timeout=0.5)
        except (OSError, serial.SerialException):
            time.sleep(0.5)
    die("%s did not come back within %d s" % (port, wait_s))


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


def exchange(port, line, wait_s=120):
    """Send the record, then wait for the node to come back on it and join Wi-Fi."""
    st = ask_state(port, wait_s)
    if st is None:
        die("the node never answered PROV? on %s -- is it running a release image?" % port)
    print("enroll: before  src=%s node=%s nets=%s fw=%s"
          % (st.get("src"), st.get("node"), st.get("nets"), st.get("fw")))
    s = open_port(port)
    s.write(line.encode() + b"\n")
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("node")
    ap.add_argument("port", help="serial port, e.g. /dev/ttyACM0")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--release", help="release tag to download and verify, e.g. v0.1.0")
    src.add_argument("--input-dir", help="an arduino-cli output dir holding hear_node.ino.bin et al.")
    src.add_argument("--no-flash", action="store_true", help="the board already runs a release image")
    ap.add_argument("--class", dest="cls", default="xiao-s3-pps")
    ap.add_argument("--wifi", default=wifi_store.PATH)
    a = ap.parse_args(argv)

    try:
        pairs = wifi_store.read_pairs(a.wifi)
        line = prov_line(a.node, a.cls, pairs)
    except (OSError, ValueError) as e:
        die(str(e))
    print("enroll: %s (%s) with %d network(s): %s"
          % (a.node, a.cls, len(pairs), ", ".join(wifi_store.mask(s) for s, _ in pairs)))

    if a.release:
        with tempfile.TemporaryDirectory() as d:
            release_files(a.release, d)
            upload(a.port, d)
    elif a.input_dir:
        upload(a.port, a.input_dir)

    ip = exchange(a.port, line)
    print("enroll: %s joined Wi-Fi at %s" % (a.node, ip))
    try:
        with urllib.request.urlopen("http://%s/status" % ip, timeout=6) as r:
            import json
            st = json.load(r)
        prov = st.get("prov") or {}
        if st.get("node") != a.node or prov.get("src") != "nvs":
            die("%s reports node=%r prov=%r" % (ip, st.get("node"), prov))
        print("enroll: OK -- %s reports node=%r fw=%r prov=%r" % (ip, st["node"], st.get("fw"), prov))
    except OSError:
        print("enroll: joined, but %s is not reachable from here to confirm /status" % ip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
