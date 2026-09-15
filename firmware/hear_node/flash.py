#!/usr/bin/env python3
"""Flash ONE node, with its identity and its destination named in the same command.

    python3 firmware/hear_node/flash.py mach 172.16.100.116                    # build here, OTA
    python3 firmware/hear_node/flash.py mach /dev/ttyACM0                      # build here, USB
    python3 firmware/hear_node/flash.py mach 172.16.100.116 --release v0.1.0   # a published image

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
the node that was asked for, or -- in a build-mode flash -- is not running the version that was
just built. A flash that silently lands the wrong identity is the failure being
fixed; catching it needs the check to be part of the same command too. Identity alone is not
enough: a node whose new image panics before HEAR_BOOT_HEALTHY_MS is reverted to its previous
slot by the boot guard and keeps answering with the same name, so only the reported fw string
separates "flashed" from "flashed, panicked and rolled back".

RELEASE MODE installs a published image instead of building one. Release images carry no
credentials and no name; the node's NVS supplies both. So the node must already be enrolled, either
by enroll.py or by any build of this tree flashed in the default mode, which copies its compiled-in
credentials into NVS at boot. A node that does not report an NVS record is refused.

RELEASE PROVENANCE. A release-mode flash refuses a tag listed in release_revocations.json before
it downloads anything, and refuses a release whose manifest declares an SBOM or a signed
attestation that is not there. `--verify-signature` additionally runs `gh attestation verify`
against the published Sigstore bundle (no GitHub credential needed); without it the printed line
says the signature was NOT checked rather than implying it was. `--allow-unattested` installs a
release whose attestation bundle is missing, and says so.

/update IS A PRIVILEGED ENDPOINT. The firmware endpoint-auth change made /update, /reboot, /format
and /gate require `X-Hear-Auth: <HEAR_ADMIN_TOKEN>`. This script is the only OTA client there is,
so it sends the token (from ~/.hear_push, never a tracked file). A release-mode flash is refused
unless live /status proves the node has admin and push credentials in NVS before any byte is
written.
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
sys.path.insert(0, HERE)
import board_profiles  # noqa: E402
import deploy_gate  # noqa: E402
import gen_secrets  # noqa: E402
import release_manifest  # noqa: E402

SKETCH = os.path.relpath(HERE, REPO)
# Kept for callers that ask for "the" FQBN. The one this script COMPILES with is chosen per node
# by board_profiles.fqbn(), because PSRAM bus mode is a property of the board in front of you and
# not of its class: gold is quad, ageev is octal, and they share the class name esp32s3-i2s-gps.
FQBN = board_profiles.FQBN
REPO_SLUG = "rjmendez/dama-hear"
# Opt-out for the tokenless-image refusal below. Spelled out rather than --force so that the thing
# being accepted -- losing remote admin on this node -- is written in the command that accepts it.
LOCKOUT_OPT_OUT = "--allow-admin-lockout"


def admin_token(path=None):
    """The admin token privileged endpoints require, from ~/.hear_push, or "" if none is set.

    Same source and same reader as gen_secrets.py, so the value this script SENDS cannot drift
    from the value it COMPILES IN: they are one line of one untracked file.
    """
    return (gen_secrets.read_push_config(path) or {}).get("HEAR_ADMIN_TOKEN", "").strip()


def admin_lockout_refusal(token, argv, node, target):
    """Why an over-the-air build-mode flash must not proceed with no admin token, or None.

    A tokenless image answers /status forever but cannot protect /update with any credential.
    The firmware keeps /update recoverable in that state, but this script still refuses to create
    the state accidentally: a field node should keep admin in NVS before any release OTA.
    """
    if token or LOCKOUT_OPT_OUT in argv:
        return None
    return ("this build has no HEAR_ADMIN_TOKEN, and the firmware fails closed: %s would come "
            "back answering /status but refusing /update, /reboot, /format and /gate from "
            "everyone -- including this script, so this would be the last flash %s can take "
            "without a USB cable. Put HEAR_ADMIN_TOKEN=<token> in ~/.hear_push (untracked, same "
            "file gen_secrets.py reads) and flash again, or pass %s if you can reach the board."
            % (node, target, LOCKOUT_OPT_OUT))


def reidentify_refusal(was, node, target, argv):
    """Why the target must not be flashed as `node`, or None.

    Flashing a node with someone else's identity is recoverable; doing it without noticing is not.
    --force is the deliberate case (re-identifying a board on purpose) and must return None, not
    an empty refusal -- an empty message is a refusal nobody can act on.
    """
    if was is None:
        return None
    got = was.get("node")
    if got in (node, None) or "--force" in argv:
        return None
    return ("%s currently reports node=%r, not %r. Refusing: name the right target, or pass "
            "--force if you really are re-identifying this board." % (target, got, node))


def ota_curl_cmd(target, bin_path, token):
    """The OTA upload command. The admin token goes in a HEADER, never the query string, so it
    does not land in a URL that gets pasted into a log or a shell history."""
    cmd = ["curl", "-s", "-m", "120", "-w", "\n%{http_code}"]
    if token:
        cmd += ["-H", "X-Hear-Auth: " + token]
    return cmd + ["-F", "firmware=@" + bin_path, "http://%s/update" % target]


def ota_post(target, bin_path, token):
    """POST the image and return (body, http_status). Status is read explicitly because a 401 is
    a different problem from a rejected image and telling an operator "OTA rejected" for a
    missing token sends them to read the wrong log."""
    r = subprocess.run(ota_curl_cmd(target, bin_path, token), capture_output=True, text=True)
    out = r.stdout.rstrip("\n")
    body, _, tail = out.rpartition("\n")
    if not (len(tail) == 3 and tail.isdigit()):
        body, tail = out, ""      # no status line came back; the body is all there is
    return body.strip(), tail, (r.stderr or "").strip()


def built_fw_version(path=None):
    """The FW_BUILD gen_secrets.py just compiled in, or None if it is not assertable.

    A build-mode flash used to be called OK on identity alone, and identity survives a revert:
    when the new image panics before HEAR_BOOT_HEALTHY_MS the boot guard rolls back to the
    previous slot, which answers /status with the SAME node id -- so the flash that put a
    panicking image on the fleet reported `flash: OK`. The version the node reports is what
    tells those two apart. Returns None (check skipped, not failed) when the value cannot mean
    anything: no secrets.h, or a placeholder git could not resolve.
    """
    path = path or os.path.join(HERE, "secrets.h")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r'^\s*#define\s+FW_BUILD\s+"([^"]*)"', text, re.M)
    if not m:
        return None
    build = m.group(1).strip()
    if build in ("", "unknown", "unset"):
        return None
    return build


def die(msg, code=1):
    print("flash: " + msg, file=sys.stderr)
    sys.exit(code)


def status(host, timeout=6):
    url = host if host.startswith("http") else "http://%s" % host
    with urllib.request.urlopen(url + "/status", timeout=timeout) as r:
        return json.load(r)


def release_refusal(st, node, board_class=None):
    """Why this node must not take a release image, or None."""
    if st.get("node") != node:
        return "it reports node=%r, not %r" % (st.get("node"), node)
    live_class = st.get("class")
    if board_class is not None:
        if live_class != board_class:
            return "it reports class=%r, not %r" % (live_class, board_class)
    elif not live_class:
        return "its /status has no class, so the correct release image cannot be chosen safely"
    else:
        try:
            board_profiles.require_board_class(live_class)
        except ValueError as e:
            return str(e)
    prov = st.get("prov")
    if not isinstance(prov, dict):
        return ("its firmware predates NVS enrollment. Flash a build of this tree first "
                "(flash.py %s <ip>); that copies its credentials into NVS" % node)
    if not prov.get("nvs") or int(prov.get("nets") or 0) < 1:
        return "NVS holds no enrolled Wi-Fi for it, so a release image would come up as its own AP"
    if not prov.get("loaded"):
        return "NVS enrollment has not been read back yet; reboot once before installing a release"
    auth = deploy_gate.auth_reasons(st, require_nvs_credentials=True)
    if auth:
        return "; ".join(auth)
    return None


def _live_psram_mode(st):
    """The PSRAM bus mode reported by /status, if this firmware is new enough to expose it."""
    for holder, key in ((st.get("sys"), "psram_bus"), (st.get("hardware"), "psram_mode"),
                        (st, "psram_mode")):
        if isinstance(holder, dict) and holder.get(key):
            return board_profiles.require_psram_mode(str(holder[key]).strip().lower())
    return None


def release_psram_mode(st, board_class, node):
    """Choose the release asset's PSRAM variant without relaxing the mismatch guard."""
    recorded = board_profiles.psram_mode(board_class, node)
    try:
        live = _live_psram_mode(st)
    except ValueError as e:
        die(str(e))
    if live is not None:
        why = board_profiles.release_variant_refusal(board_class, node, live)
        if why:
            die("refusing to install release on %s: live /status reports %s PSRAM but %s"
                % (st.get("node"), live, why))
        return live
    sys_block = st.get("sys") if isinstance(st.get("sys"), dict) else {}
    if sys_block.get("psram_fault") and recorded == board_profiles.psram_mode(board_class):
        die("refusing to guess a PSRAM release variant for %s: /status reports psram_fault but no "
            "live psram_bus; record this node in NODE_PSRAM_MODES first" % node)
    return recorded


def resolve_board_class(requested, live_status=None, require_live=False):
    if requested is not None:
        board_profiles.require_board_class(requested)
    live_class = None if live_status is None else live_status.get("class")
    if requested and live_class and live_class != requested:
        die("refusing class=%r: %r currently reports class=%r" % (requested, live_status.get("node"), live_class))
    chosen = requested or live_class or board_profiles.DEFAULT_BOARD_CLASS
    try:
        return board_profiles.require_board_class(chosen)
    except ValueError as e:
        if require_live or live_class:
            die(str(e))
        raise


def release_image(tag, board_class, psram_mode=None, repo=REPO_SLUG, allow_unattested=False,
                  verify_signature=False):
    """Download the board-class/PSRAM app image, refuse it unless release metadata vouches for it.

    Three checks, in this order, and any one of them refuses before a byte is written to the node:

      1. The tag is not revoked (release_revocations.json in THIS checkout -- an installer that
         has not pulled gets the stale answer, which is why a rollout starts with a pull).
      2. The manifest's hash for this exact asset matches what was downloaded, and the release
         does not claim to carry compiled-in credentials.
      3. The SBOM and the signed attestation the manifest declares exist and cover this asset.
         The signature itself is checked only with --verify-signature, which needs `gh`; without
         it the printed line says so rather than implying a signature was checked.
    """
    import enroll
    base = "https://github.com/%s/releases/download/%s/" % (repo, tag)
    psram_mode = board_profiles.require_psram_mode(psram_mode or board_profiles.psram_mode(board_class))
    name = board_profiles.release_asset_name(tag, board_class, "app", psram_mode)
    revoked = release_manifest.revocation_refusal(tag)
    if revoked:
        die("%s. Nothing was downloaded." % revoked)
    bundle = None
    try:
        manifest_text = None
        try:
            manifest_text = enroll.fetch(base + release_manifest.MANIFEST_NAME).decode()
        except OSError:
            pass
        data = enroll.fetch(base + name)
        if manifest_text is not None:
            manifest = json.loads(manifest_text)
            sbom, bundle = release_manifest.fetch_provenance_assets(
                enroll.fetch, base, manifest, allow_unattested=allow_unattested)
            # The TEXT, not the parsed dict: the attestation's subject digest for
            # release-manifest.json is over the bytes that were downloaded.
            release_manifest.verify_downloaded_release_assets(
                manifest_text, tag, board_class, {name: data}, psram_mode=psram_mode,
                sbom_source=sbom, attestation_bundle=bundle)
        else:
            sums = enroll.fetch(base + "SHA256SUMS").decode()
            enroll.check_sums(sums, name, data)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        die("release %s: %s" % (tag, e))
    variant = board_profiles.build_variant(board_class) if psram_mode == board_profiles.psram_mode(board_class) \
        else board_class + board_profiles.PSRAM_MODES[psram_mode]["variant_suffix"]
    d = os.path.join(REPO, ".otabuild", "release-%s-%s" % (tag, variant))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "wb") as f:
        f.write(data)
    signature_verified = False
    if bundle is not None and verify_signature:
        bundle_path = os.path.join(d, release_manifest.ATTESTATION_BUNDLE_NAME)
        with open(bundle_path, "wb") as f:
            f.write(bundle)
        try:
            release_manifest.verify_attestation_signature(bundle_path, path, repository=repo)
            signature_verified = True
        except (ValueError, RuntimeError) as e:
            die("release %s: %s" % (tag, e))
    if manifest_text is None:
        stamp = "SHA256SUMS (legacy release)"
    else:
        stamp = release_manifest.describe_verification(
            json.loads(manifest_text), bundle is not None, signature_verified)
    print("flash: %s verified against %s (%d B)" % (name, stamp, len(data)))
    return path


def main(argv):
    board_class = None
    release = None
    if "--release" in argv:
        i = argv.index("--release")
        if i + 1 >= len(argv):
            die("--release needs a tag")
        release = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if "--class" in argv:
        i = argv.index("--class")
        if i + 1 >= len(argv):
            die("--class needs a board class")
        board_class = argv[i + 1].strip()
        argv = argv[:i] + argv[i + 2:]
    # Provenance switches. Flags, not values, so nothing here can land in shell history or `ps`.
    allow_unattested = "--allow-unattested" in argv
    verify_signature = "--verify-signature" in argv
    pos = [a for a in argv[1:] if not a.startswith("--")]
    if len(pos) < 2:
        print(__doc__.strip())
        return 2
    node, target = pos[0].strip(), pos[1].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,22}", node):
        die("node id must be lowercase letters, digits and dashes: %r" % node)

    is_serial = target.startswith("/dev/")
    was = None
    expect_fw = None

    if release:
        if is_serial:
            die("a release goes over the air; a board on USB is enrolled with enroll.py")
        try:
            was = status(target)
        except Exception as e:
            die("%s is not answering /status (%s); a release needs a running, enrolled node"
                % (target, e))
        board_class = resolve_board_class(board_class, was, require_live=True)
        why = release_refusal(was, node, board_class)
        if why:
            die("refusing to install %s on %s: %s" % (release, target, why))
        mode = release_psram_mode(was, board_class, node)
        why = board_profiles.release_variant_refusal(board_class, node, mode)
        if why:
            die("refusing to install %s on %s: %s" % (release, target, why))
        bin_path = release_image(release, board_class, mode,
                                 allow_unattested=allow_unattested,
                                 verify_signature=verify_signature)
    else:
        # 1. identity, for THIS node, immediately before the build that carries it
        if not is_serial:
            try:
                was = status(target)
            except Exception:
                was = None          # not up yet, or first flash. Not a reason to refuse.
        board_class = resolve_board_class(board_class, was)
        try:
            variant = board_profiles.build_variant(board_class, node)
            node_fqbn = board_profiles.fqbn(board_class, node)
        except ValueError as e:
            die(str(e))
        outdir = os.path.join(REPO, ".otabuild", "%s-%s" % (node, variant))

        subprocess.run([sys.executable, os.path.join(HERE, "gen_secrets.py"), node, board_class],
                       cwd=REPO, check=True)
        # An image with no admin token cannot be replaced over the air by its own successor. Check
        # before the compile, not after: an operator who has to add a token should find that out in
        # two seconds, not two minutes.
        if not is_serial:
            why = admin_lockout_refusal(admin_token(), argv, node, target)
            if why:
                die("refusing to flash %s over the air: %s" % (node, why))
        # What this build will report as fw, read from the secrets.h just written. Checked at
        # step 5 so a boot-guard revert cannot pass as a successful flash.
        expect_fw = built_fw_version()

        # 2. if the target is already reachable, refuse a target that is a DIFFERENT node.
        why = reidentify_refusal(was, node, target, argv)
        if why:
            die(why)
        if was is not None and was.get("node") not in (node, None):
            print("flash: --force -- %s reports node=%r and is being re-identified as %r"
                  % (target, was.get("node"), node))

        # 3. build
        print("flash: building %s (%s, %s PSRAM) for %s"
              % (node, board_class, board_profiles.psram_mode(board_class, node), target))
        # --libraries: the shared platform code lives in firmware/lib/hear_platform and
        # arduino-cli will not find it otherwise.
        cmd = ["arduino-cli", "compile", "--fqbn", node_fqbn,
               "--libraries", os.path.join(REPO, "firmware", "lib"),
               "--output-dir", outdir]
        flags = board_profiles.build_extra_flags(board_class)
        if flags:
            cmd += ["--build-property", "compiler.cpp.extra_flags=" + flags]
        cmd += [SKETCH]
        r = subprocess.run(cmd, cwd=REPO)
        if r.returncode:
            die("compile failed")

        bin_path = os.path.join(outdir, "hear_node.ino.bin")
        if not os.path.exists(bin_path):
            die("no image at %s" % bin_path)

    # 4. flash
    if is_serial:
        upload_fqbn = board_profiles.fqbn(board_class, node) if board_class else FQBN
        r = subprocess.run(["arduino-cli", "upload", "-p", target, "--fqbn", upload_fqbn,
                            "--input-dir", outdir, SKETCH], cwd=REPO)
        if r.returncode:
            die("upload failed")
    else:
        body, code, err = ota_post(target, bin_path, admin_token())
        print("flash: %s" % body)
        if code == "401":
            die("OTA refused by %s with 401: /update requires the admin token. Put "
                "HEAR_ADMIN_TOKEN=<token> in ~/.hear_push -- it must be the value the RUNNING "
                "image was built with, not the one you are about to flash -- and try again. If "
                "that value is lost, this node can only be reflashed over USB." % target)
        if "OK" not in body:
            die("OTA rejected (HTTP %s): %s%s" % (code or "?", body, err))

    # 5. verify the node that comes back is the node that was asked for
    host = target if not is_serial else None
    if host is None:
        print("flash: serial upload done; verify with  curl http://%s.local/status" % node)
        return 0
    stale_fw = None
    for _ in range(40):
        time.sleep(3)
        try:
            now = status(host)
        except Exception:
            continue
        got = now.get("node")
        if got != node:
            die("FLASHED THE WRONG IDENTITY: %s now reports node=%r, expected %r" % (host, got, node))
        prov = now.get("prov") or {}
        if release and (now.get("fw") != release or prov.get("src") != "nvs"):
            if now.get("uptime_s", 99) > 60:
                die("%s came back as %r but reports fw=%r prov=%r, not %s on its NVS record"
                    % (host, got, now.get("fw"), prov, release))
            continue          # still the old image, answering before the reboot
        if release:
            auth = deploy_gate.auth_reasons(now, require_nvs_credentials=True, require_push_success=True)
            if auth:
                if any("401" in reason for reason in auth) or now.get("uptime_s", 0) > 90:
                    die("%s is running %s but failed the authentication gate: %s"
                        % (host, release, "; ".join(auth)))
                continue
        if not release and expect_fw and now.get("fw") != expect_fw:
            # Identity matches and the version does not: either the reboot has not landed yet,
            # or the node ran the new image, panicked and the boot guard put the old one back.
            # Both look identical for the first seconds, so keep polling and only call it a
            # failure once the window closes.
            stale_fw = now.get("fw")
            continue
        print("flash: OK -- %s reports node=%r class=%r fw=%r prov=%r uptime=%ss"
              % (host, got, now.get("class"), now.get("fw"), prov, now.get("uptime_s")))
        return 0
    if stale_fw is not None:
        die("%s answers as %r but still reports fw=%r, not the %r just flashed -- the image did "
            "not stay up (a panic before HEAR_BOOT_HEALTHY_MS makes the boot guard revert to the "
            "previous slot, which keeps the same identity). Read its /log before reflashing."
            % (host, node, stale_fw, expect_fw))
    die("node did not come back within 120 s -- check it before flashing anything else")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
