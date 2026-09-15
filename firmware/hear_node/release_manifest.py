#!/usr/bin/env python3
"""Generate and verify immutable hear_node release manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from typing import Iterable

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import board_profiles  # noqa: E402

MANIFEST_NAME = "release-manifest.json"
# What a published image IS, as a field rather than as prose in the release notes. "unprovisioned"
# is the only value a public GitHub release may carry: the image has no Wi-Fi credentials, no node
# name, no admin token and no push token compiled into it, and a node supplies all four from its
# NVS record. An asset that claimed anything else would be an asset with a live fleet credential
# inside it, which is why generate refuses to write one.
IMAGE_CLASS_UNPROVISIONED = "unprovisioned"
PROVISIONING_REQUIRED = ("node_id", "wifi", "admin_token", "push_token")
# A tree that still has secrets.h may have compiled it in. Refusing here is cheaper than
# discovering it after the assets are public and the tokens have to be rotated across the fleet.
SECRETS_HEADER = "firmware/hear_node/secrets.h"
SCHEMA_NAME = "release-manifest.schema.json"
SCHEMA_VERSION = 1
MANIFEST_TYPE = "dama-hear-hear_node-release"
UPLOAD_KINDS = ("app", "bootloader", "partitions", "merged", "elf")
COMMON_INPUTS = (
    (".github/workflows/firmware.yml", "ci-build-workflow"),
    (".github/workflows/release.yml", "release-publish-workflow"),
    ("firmware/hear_node/board_profiles.py", "board-class-release-map"),
    ("firmware/hear_node/deploy_gate.py", "rollout-readiness-gate"),
    ("firmware/hear_node/enroll.py", "usb-release-installer"),
    ("firmware/hear_node/flash.py", "ota-release-installer"),
    ("firmware/hear_node/hear_node.ino", "firmware-sketch"),
    ("firmware/lib/hear_platform/src/hear_prov.h", "nvs-provenance-storage"),
    ("firmware/lib/hear_platform/src/hear_prov_line.h", "provisioning-wire-format"),
)
GENERATED_INPUTS = (
    ("firmware/hear_node/decim.h", "firmware/gen_decim.py", "generated-decimator"),
    ("firmware/hear_node/mel_impulse.h", "firmware/gen_mel.py", "generated-impulse-mel-bank"),
    ("firmware/hear_node/mel_scene.h", "firmware/gen_mel_scene.py", "generated-scene-mel-bank"),
)
SCHEMA_GUARDS = (
    ("firmware/hear_node/hear_push_payload.h", "heartbeat-event-schema-source"),
    ("tests/test_firmware_csv_schema.py", "csv-schema-regression-guard"),
)
HEADER_RE = re.compile(r"^\s*#define\s+([A-Z0-9_]+)\s+(.+?)\s*(?://.*)?$", re.M)


def die(msg, code=1):
    raise SystemExit(msg if code == 1 else code)


def _json_text(obj):
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: pathlib.Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _record_bytes(name: str, data: bytes, **extra):
    out = {"name": name, "bytes": len(data), "sha256": _sha256_bytes(data)}
    out.update(extra)
    return out


def _record_path(root: pathlib.Path, rel: str, **extra):
    path = root / rel
    if not path.exists():
        raise ValueError("%s is missing" % rel)
    out = {"path": rel, "bytes": path.stat().st_size, "sha256": _sha256_path(path)}
    out.update(extra)
    return out


def _record_release_path(dist_dir: pathlib.Path, name: str, **extra):
    path = dist_dir / name
    if not path.exists():
        raise ValueError("%s is missing" % name)
    out = {"name": name, "bytes": path.stat().st_size, "sha256": _sha256_path(path)}
    out.update(extra)
    return out


def _git(repo_root: pathlib.Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_root), *args], text=True).strip()


def _normalise_rel(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _ignored(rel: str, ignore_paths: Iterable[str]) -> bool:
    rel = _normalise_rel(rel)
    for raw in ignore_paths:
        item = _normalise_rel(raw)
        if not item:
            continue
        if rel == item or rel.startswith(item + "/"):
            return True
    return False


def _safe_relative(path: pathlib.Path, root: pathlib.Path):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def git_source_state(repo_root: pathlib.Path, ignore_paths: Iterable[str] = ()):
    commit = _git(repo_root, "rev-parse", "HEAD")
    short = _git(repo_root, "rev-parse", "--short", "HEAD")
    clean_describe = _git(repo_root, "describe", "--always", "--tags")
    dirty_paths = []
    for line in _git(repo_root, "status", "--porcelain", "--untracked-files=all").splitlines():
        rel = line[3:].strip().strip('"')
        if rel and not _ignored(rel, ignore_paths):
            dirty_paths.append(_normalise_rel(rel))
    dirty_paths = sorted(set(dirty_paths))
    describe = clean_describe + ("-dirty" if dirty_paths else "")
    return {
        "repository": "rjmendez/dama-hear",
        "commit": commit,
        "commit_short": short,
        "describe": describe,
        "dirty": bool(dirty_paths),
        "dirty_paths": dirty_paths,
    }


def _parse_macros(path: pathlib.Path):
    return {k: v.strip() for k, v in HEADER_RE.findall(path.read_text(encoding="utf-8"))}


def _macro_string(macros, name):
    try:
        value = macros[name]
    except KeyError as e:
        raise ValueError("missing %s" % name) from e
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _macro_int(macros, name, default=None):
    if name not in macros:
        if default is not None:
            return default
        raise ValueError("missing %s" % name)
    value = macros[name].strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    return int(value, 0)


def _gps_name(token: str) -> str:
    return {
        "GPS_UBX": "ubx",
        "GPS_PMTK": "pmtk",
    }.get(token, token.lower())


def _mic_name(token: str) -> str:
    return {
        "MIC_PDM": "pdm",
        "MIC_I2S": "i2s",
    }.get(token, token.lower())


def load_build_info(path: pathlib.Path):
    info = json.loads(path.read_text(encoding="utf-8"))
    required = ("tag", "commit", "sketch", "fqbn", "esp32_core", "arduino_cli", "variants")
    missing = [k for k in required if k not in info]
    if missing:
        raise ValueError("build-info.json is missing %s" % ", ".join(missing))
    image_class = info.get("image_class", IMAGE_CLASS_UNPROVISIONED)
    if image_class != IMAGE_CLASS_UNPROVISIONED:
        raise ValueError("build-info.json declares image_class=%r; a published release must be %r"
                         % (image_class, IMAGE_CLASS_UNPROVISIONED))
    if info.get("compiled_in_credentials"):
        raise ValueError("build-info.json says the images carry compiled-in credentials; a public "
                         "release asset must never contain a fleet token")
    return info


def _expected_build_flags(board_class: str):
    return ["-DHEAR_ALLOW_NO_WIFI", *board_profiles.BOARD_PROFILES[board_class]["cpp_flags"]]


def _variant_from_build_info(info, board_class: str, psram_mode: str):
    for variant in info["variants"]:
        if variant.get("board_class") == board_class and variant.get("psram_mode") == psram_mode:
            return variant
    raise ValueError("build-info.json has no %s/%s variant" % (board_class, psram_mode))


def _release_variants_from_build_info(info):
    variants = []
    seen = set()
    for item in info["variants"]:
        stem = item.get("release_stem")
        if not stem:
            continue
        board_class = item.get("board_class")
        psram_mode = item.get("psram_mode")
        board_profiles.require_board_class(board_class)
        board_profiles.require_psram_mode(psram_mode)
        key = (board_class, psram_mode)
        if key in seen:
            raise ValueError("build-info.json repeats release variant %s/%s" % key)
        seen.add(key)
        variants.append(key)
    return variants


def capture_profile(repo_root: pathlib.Path, board_class: str):
    header_rel = board_profiles.board_header(board_class)
    header = _parse_macros(repo_root / header_rel)
    sketch = _parse_macros(repo_root / "firmware/hear_node/hear_node.ino")
    fs_nominal = _macro_int(header, "FS_NOMINAL")
    decim = _macro_int(sketch, "DECIM")
    return {
        "board_name": _macro_string(header, "BOARD_NAME"),
        "board_header": header_rel,
        "gps_protocol": _gps_name(_macro_string(header, "GPS_PROTO")),
        "mic_kind": _mic_name(_macro_string(header, "MIC_KIND")),
        "mic_count": _macro_int(header, "MIC_COUNT", default=1),
        "fs_nominal_hz": fs_nominal,
        "decimation": decim,
        "fs_acquisition_hz": fs_nominal * decim,
        "mic_band_lo_hz": _macro_int(header, "MIC_BAND_LO_HZ"),
        "mic_band_hi_hz": _macro_int(header, "MIC_BAND_HI_HZ"),
    }


def _release_artifact(dist_dir: pathlib.Path, name: str, kind: str):
    return _record_release_path(dist_dir, name, kind=kind)


def _variant_manifest(repo_root: pathlib.Path, dist_dir: pathlib.Path, tag: str, build_info,
                      board_class: str, psram_mode: str):
    prof = capture_profile(repo_root, board_class)
    variant_info = _variant_from_build_info(build_info, board_class, psram_mode)
    expected_stem = board_profiles.release_stem(board_class, psram_mode)
    if variant_info.get("release_stem") != expected_stem:
        raise ValueError("build-info.json release_stem for %s/%s is %r, expected %r"
                         % (board_class, psram_mode, variant_info.get("release_stem"), expected_stem))
    build_flags = variant_info.get("build_flags") or []
    if build_flags != _expected_build_flags(board_class):
        raise ValueError("build-info.json build_flags for %s/%s are %r, expected %r"
                         % (board_class, psram_mode, build_flags, _expected_build_flags(board_class)))
    if prof["board_name"] != board_class:
        raise ValueError("%s declares BOARD_NAME=%r, expected %r"
                         % (prof["board_header"], prof["board_name"], board_class))
    artifacts = []
    for kind in UPLOAD_KINDS:
        artifacts.append(_release_artifact(
            dist_dir,
            board_profiles.release_asset_name(tag, board_class, kind, psram_mode),
            kind))
    partitions = next(a for a in artifacts if a["kind"] == "partitions")
    return {
        "board_class": board_class,
        "psram_mode": psram_mode,
        "release_stem": expected_stem,
        "fqbn": variant_info.get("fqbn"),
        "build_flags": build_flags,
        "board_header": _record_path(repo_root, board_profiles.board_header(board_class)),
        "capture_profile": prof,
        "partition_table": {
            "artifact": partitions["name"],
            "sha256": partitions["sha256"],
            "bytes": partitions["bytes"],
            "source_path": None,
            "source_sha256": None,
            "source_status": "binary-only: hear_node uses the board package partition layout at build time",
        },
        "artifacts": artifacts,
    }


def build_manifest(repo_root: pathlib.Path, dist_dir: pathlib.Path, build_info_path: pathlib.Path,
                   allow_dirty=False):
    build_info = load_build_info(build_info_path)
    ignore = ()
    rel_dist = _safe_relative(dist_dir, repo_root)
    if rel_dist:
        ignore = (rel_dist,)
    state = git_source_state(repo_root, ignore_paths=ignore)
    tag = build_info["tag"]
    dirty_reasons = []
    if state["commit"] != build_info["commit"]:
        raise ValueError("build-info.json commit %s does not match source HEAD %s"
                         % (build_info["commit"], state["commit"]))
    if not state["dirty"] and state["describe"] != tag:
        raise ValueError("source describes itself as %r, not release tag %r"
                         % (state["describe"], tag))
    if state["dirty"]:
        dirty_reasons.append("source tree is dirty: %s" % ", ".join(state["dirty_paths"]))
    if dirty_reasons and not allow_dirty:
        raise ValueError("refusing unverifiable release manifest: %s" % "; ".join(dirty_reasons))
    if (repo_root / SECRETS_HEADER).exists():
        raise ValueError("refusing to publish a release built from a tree that still has %s: the "
                         "images may carry compiled-in Wi-Fi credentials and fleet tokens"
                         % SECRETS_HEADER)

    inputs = [_record_path(repo_root, rel, role=role) for rel, role in COMMON_INPUTS]
    generated_files = []
    for rel, generator, role in GENERATED_INPUTS:
        rec = _record_path(repo_root, rel, role=role)
        rec["generator"] = generator
        rec["generator_sha256"] = _sha256_path(repo_root / generator)
        generated_files.append(rec)
    schema_guards = [_record_path(repo_root, rel, role=role) for rel, role in SCHEMA_GUARDS]
    variants = [_variant_manifest(repo_root, dist_dir, tag, build_info, board_class, psram_mode)
                for board_class, psram_mode in _release_variants_from_build_info(build_info)]

    schema_obj = schema_document()
    schema_text = _json_text(schema_obj)
    build_info_bytes = build_info_path.read_bytes()
    release_artifacts = [
        _record_bytes("build-info.json", build_info_bytes, kind="build-info"),
        _record_bytes(SCHEMA_NAME, schema_text.encode("utf-8"), kind="manifest-schema"),
    ]

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_type": MANIFEST_TYPE,
        "tag": tag,
        "firmware_version": tag,
        "source": {
            **state,
            "verifiable": not dirty_reasons,
            "refusals": dirty_reasons,
        },
        "build": {
            "sketch": build_info["sketch"],
            "fqbn": build_info["fqbn"],
            "libraries": ["firmware/lib"],
            "arduino_cli_version": build_info["arduino_cli"],
            "esp32_core_version": build_info["esp32_core"],
            "credentials_policy": build_info.get("credentials", ""),
            # The installer contract, in the manifest the installer already verifies: this image
            # is node-ready only for a node that already holds these four things in NVS.
            "image_class": IMAGE_CLASS_UNPROVISIONED,
            "compiled_in_credentials": False,
            "provisioning_required": list(PROVISIONING_REQUIRED),
        },
        "inputs": inputs,
        "generated_files": generated_files,
        "schema_guards": schema_guards,
        "variants": variants,
        "release_artifacts": release_artifacts,
    }
    return manifest, schema_text


def write_manifest(repo_root: pathlib.Path, dist_dir: pathlib.Path, build_info_path: pathlib.Path,
                   allow_dirty=False):
    manifest, schema_text = build_manifest(repo_root, dist_dir, build_info_path, allow_dirty=allow_dirty)
    (dist_dir / SCHEMA_NAME).write_text(schema_text, encoding="utf-8")
    text = _json_text(manifest)
    (dist_dir / MANIFEST_NAME).write_text(text, encoding="utf-8")
    return manifest


def _load_manifest(source):
    if isinstance(source, dict):
        return source
    if isinstance(source, pathlib.Path):
        return json.loads(source.read_text(encoding="utf-8"))
    if isinstance(source, bytes):
        return json.loads(source.decode("utf-8"))
    if isinstance(source, str):
        return json.loads(source)
    raise TypeError("cannot load manifest from %r" % type(source))


_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _type_matches(value, want: str) -> bool:
    expected = _JSON_TYPES[want]
    if want in ("integer", "number") and isinstance(value, bool):
        return False
    if want == "boolean":
        return isinstance(value, bool)
    return isinstance(value, expected)


def _schema_errors(value, schema, where: str):
    """Validate against the subset of JSON Schema that schema_document() uses."""
    problems = []
    if "const" in schema and value != schema["const"]:
        problems.append("%s must be %r, got %r" % (where, schema["const"], value))
        return problems
    types = schema.get("type")
    if types is not None:
        wanted = types if isinstance(types, list) else [types]
        if not any(_type_matches(value, t) for t in wanted):
            problems.append("%s must be of type %s" % (where, " or ".join(wanted)))
            return problems
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append("%s is missing required field %r" % (where, key))
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                problems.extend(_schema_errors(value[key], sub, "%s.%s" % (where, key)))
    elif isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            problems.extend(_schema_errors(item, schema["items"], "%s[%d]" % (where, i)))
    elif isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, value):
            problems.append("%s %r does not match %s" % (where, value, pattern))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            problems.append("%s must be >= %s" % (where, minimum))
    return problems


def manifest_schema_problems(manifest):
    """Problems that make a manifest document unusable, as plain strings."""
    if not isinstance(manifest, dict):
        return ["release manifest must be a JSON object, got %s" % type(manifest).__name__]
    return _schema_errors(manifest, schema_document(), "manifest")


def validate_manifest_document(manifest):
    """Refuse any manifest that does not satisfy the published schema."""
    problems = manifest_schema_problems(manifest)
    if problems:
        shown, extra = problems[:6], len(problems) - 6
        detail = "; ".join(shown) + (" (+%d more)" % extra if extra > 0 else "")
        raise ValueError("release manifest does not match %s: %s" % (SCHEMA_NAME, detail))
    return manifest


def _artifact_index(variant, board_class: str):
    """Artifacts keyed by name; refuse structurally incomplete entries."""
    index = {}
    for i, item in enumerate(variant.get("artifacts", [])):
        where = "variant %s artifact[%d]" % (board_class, i)
        if not isinstance(item, dict):
            raise ValueError("%s is not an object" % where)
        for field in ("name", "sha256", "bytes"):
            if field not in item:
                raise ValueError("%s is missing required field %r" % (where, field))
        name, digest, size = item["name"], item["sha256"], item["bytes"]
        if not isinstance(name, str) or not name:
            raise ValueError("%s has a non-string name %r" % (where, name))
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("%s (%s) has a malformed sha256 %r" % (where, name, digest))
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("%s (%s) has a malformed byte count %r" % (where, name, size))
        index[name] = item
    return index


def _variant_lookup(manifest, board_class: str, psram_mode: str | None = None):
    if psram_mode is None:
        psram_mode = board_profiles.psram_mode(board_class)
    for variant in manifest.get("variants", []):
        if variant.get("board_class") == board_class and variant.get("psram_mode") == psram_mode:
            return variant
    raise ValueError("release manifest has no %s/%s variant" % (board_class, psram_mode))


def _check_hash(name: str, data: bytes, want: str):
    got = _sha256_bytes(data)
    if got != want:
        raise ValueError("%s sha256 %s, manifest says %s" % (name, got, want))


def image_provisioning_refusal(manifest):
    """Why a downloaded release image must not be installed at all, or None.

    Not "does this node have credentials" -- that is the installer's question -- but "does this
    PUBLIC ASSET claim to carry any". It must not. A release whose manifest says its images have
    credentials compiled in is a release that published a fleet token, and installing it would
    spread that token to every node that takes the update.
    """
    build = manifest.get("build")
    if not isinstance(build, dict):
        return None                     # legacy manifest: no claim either way
    if build.get("compiled_in_credentials"):
        return ("its manifest says the images carry compiled-in credentials; a public release "
                "asset must never contain a fleet token")
    image_class = build.get("image_class")
    if image_class is not None and image_class != IMAGE_CLASS_UNPROVISIONED:
        return ("its manifest declares image_class=%r, and this installer only installs %r images"
                % (image_class, IMAGE_CLASS_UNPROVISIONED))
    return None


def verify_downloaded_release_assets(manifest_source, tag: str, board_class: str,
                                     assets: dict[str, bytes], psram_mode: str | None = None):
    try:
        manifest = _load_manifest(manifest_source)
    except json.JSONDecodeError as e:
        raise ValueError("release manifest is not valid JSON: %s" % e) from e
    validate_manifest_document(manifest)
    if manifest.get("manifest_type") != MANIFEST_TYPE:
        raise ValueError("unexpected manifest_type %r" % manifest.get("manifest_type"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version %r" % manifest.get("schema_version"))
    if manifest.get("tag") != tag:
        raise ValueError("manifest tag %r does not match requested %r" % (manifest.get("tag"), tag))
    source = manifest.get("source") or {}
    if source.get("dirty"):
        raise ValueError("manifest refuses dirty source commit %s" % source.get("describe", source.get("commit")))
    if not source.get("verifiable", True):
        raise ValueError("manifest is marked unverifiable: %s"
                         % "; ".join(source.get("refusals") or ["no reason given"]))
    why = image_provisioning_refusal(manifest)
    if why:
        raise ValueError("refusing release %s: %s" % (tag, why))
    variant = _variant_lookup(manifest, board_class, psram_mode)
    expected = _artifact_index(variant, board_class)
    missing = sorted(set(assets) - set(expected))
    if missing:
        raise ValueError("manifest does not declare %s for board_class %s"
                         % (", ".join(missing), board_class))
    for name, data in assets.items():
        _check_hash(name, data, expected[name]["sha256"])
    return {
        "tag": manifest["tag"],
        "commit": source.get("commit"),
        "board_class": board_class,
        "psram_mode": variant.get("psram_mode"),
        "verified_assets": sorted(assets),
    }


def verify_release_directory(manifest_path: pathlib.Path, dist_dir: pathlib.Path,
                             expected_tag=None, expected_board_class=None, expected_psram_mode=None,
                             source_root=None):
    try:
        manifest = _load_manifest(manifest_path)
    except json.JSONDecodeError as e:
        raise ValueError("release manifest is not valid JSON: %s" % e) from e
    validate_manifest_document(manifest)
    problems = []
    if manifest.get("manifest_type") != MANIFEST_TYPE:
        problems.append("unexpected manifest_type %r" % manifest.get("manifest_type"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append("unsupported schema_version %r" % manifest.get("schema_version"))
    if expected_tag and manifest.get("tag") != expected_tag:
        problems.append("manifest tag %r does not match %r" % (manifest.get("tag"), expected_tag))
    source = manifest.get("source") or {}
    if source.get("dirty"):
        problems.append("manifest is marked dirty: %s"
                        % ", ".join(source.get("dirty_paths") or [source.get("describe", "dirty")]))
    if not source.get("verifiable", True):
        problems.append("manifest is marked unverifiable: %s"
                        % "; ".join(source.get("refusals") or ["no reason given"]))
    why = image_provisioning_refusal(manifest)
    if why:
        problems.append("release %s: %s" % (manifest.get("tag"), why))
    if expected_psram_mode and not expected_board_class:
        problems.append("--psram-mode requires --board-class")
        variant_checks = []
    elif expected_board_class:
        variant_checks = [(expected_board_class, expected_psram_mode)]
    else:
        variant_checks = [(v.get("board_class"), v.get("psram_mode")) for v in manifest.get("variants", [])]
    for board_class, psram_mode in variant_checks:
        try:
            variant = _variant_lookup(manifest, board_class, psram_mode)
        except ValueError as e:
            problems.append(str(e))
            continue
        for item in variant.get("artifacts", []):
            path = dist_dir / item["name"]
            if not path.exists():
                problems.append("missing artifact %s" % item["name"])
                continue
            if path.stat().st_size != item["bytes"]:
                problems.append("%s is %d B, manifest says %d"
                                % (item["name"], path.stat().st_size, item["bytes"]))
                continue
            if _sha256_path(path) != item["sha256"]:
                problems.append("%s sha256 does not match manifest" % item["name"])
    for item in manifest.get("release_artifacts", []):
        path = dist_dir / item["name"]
        if not path.exists():
            problems.append("missing release metadata artifact %s" % item["name"])
            continue
        if path.stat().st_size != item["bytes"]:
            problems.append("%s is %d B, manifest says %d"
                            % (item["name"], path.stat().st_size, item["bytes"]))
            continue
        if _sha256_path(path) != item["sha256"]:
            problems.append("%s sha256 does not match manifest" % item["name"])
    if source_root:
        source_root = pathlib.Path(source_root)
        ignore = ()
        rel_dist = _safe_relative(dist_dir, source_root)
        if rel_dist:
            ignore = (rel_dist,)
        state = git_source_state(source_root, ignore_paths=ignore)
        if state["commit"] != source.get("commit"):
            problems.append("source HEAD %s does not match manifest commit %s"
                            % (state["commit"], source.get("commit")))
        if state["dirty"] != bool(source.get("dirty")):
            problems.append("source dirty=%s but manifest says dirty=%s"
                            % (state["dirty"], source.get("dirty")))
        for section in ("inputs", "generated_files", "schema_guards"):
            for item in manifest.get(section, []):
                path = source_root / item["path"]
                if not path.exists():
                    problems.append("missing source input %s" % item["path"])
                    continue
                if path.stat().st_size != item["bytes"]:
                    problems.append("%s is %d B, manifest says %d"
                                    % (item["path"], path.stat().st_size, item["bytes"]))
                    continue
                if _sha256_path(path) != item["sha256"]:
                    problems.append("%s sha256 does not match manifest" % item["path"])
    if problems:
        raise ValueError("\n".join(problems))
    return {
        "tag": manifest["tag"],
        "board_classes": [board_class for board_class, _ in variant_checks],
        "artifacts_checked": sum(len(_variant_lookup(manifest, cls, mode)["artifacts"])
                                 for cls, mode in variant_checks)
                             + len(manifest.get("release_artifacts", [])),
        "source_checked": bool(source_root),
    }


def schema_document():
    file_record = {
        "type": "object",
        "required": ["bytes", "sha256"],
        "properties": {
            "bytes": {"type": "integer", "minimum": 0},
            "generator": {"type": "string"},
            "generator_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "kind": {"type": "string"},
            "name": {"type": "string"},
            "path": {"type": "string"},
            "role": {"type": "string"},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "additionalProperties": True,
    }
    named_file_record = {
        **file_record,
        "required": ["name", "bytes", "sha256"],
    }
    path_file_record = {
        **file_record,
        "required": ["path", "bytes", "sha256"],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "dama-hear hear_node release manifest",
        "type": "object",
        "required": [
            "schema_version", "manifest_type", "tag", "firmware_version", "source", "build",
            "inputs", "generated_files", "schema_guards", "variants", "release_artifacts",
        ],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "manifest_type": {"const": MANIFEST_TYPE},
            "tag": {"type": "string"},
            "firmware_version": {"type": "string"},
            "source": {
                "type": "object",
                "required": [
                    "repository", "commit", "commit_short", "describe", "dirty", "dirty_paths",
                    "verifiable", "refusals",
                ],
                "properties": {
                    "repository": {"type": "string"},
                    "commit": {"type": "string"},
                    "commit_short": {"type": "string"},
                    "describe": {"type": "string"},
                    "dirty": {"type": "boolean"},
                    "dirty_paths": {"type": "array", "items": {"type": "string"}},
                    "verifiable": {"type": "boolean"},
                    "refusals": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": True,
            },
            "build": {
                "type": "object",
                "required": [
                    "sketch", "fqbn", "libraries", "arduino_cli_version", "esp32_core_version",
                    "credentials_policy",
                ],
                "properties": {
                    "sketch": {"type": "string"},
                    "fqbn": {"type": "string"},
                    "libraries": {"type": "array", "items": {"type": "string"}},
                    "arduino_cli_version": {"type": "string"},
                    "esp32_core_version": {"type": "string"},
                    "credentials_policy": {"type": "string"},
                    "image_class": {"type": "string"},
                    "compiled_in_credentials": {"type": "boolean"},
                    "provisioning_required": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": True,
            },
            "inputs": {"type": "array", "items": path_file_record},
            "generated_files": {"type": "array", "items": path_file_record},
            "schema_guards": {"type": "array", "items": path_file_record},
            "variants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "board_class", "psram_mode", "release_stem", "fqbn", "build_flags", "board_header",
                        "capture_profile", "partition_table", "artifacts",
                    ],
                    "properties": {
                        "board_class": {"type": "string"},
                        "psram_mode": {"type": "string"},
                        "release_stem": {"type": "string"},
                        "fqbn": {"type": "string"},
                        "build_flags": {"type": "array", "items": {"type": "string"}},
                        "board_header": path_file_record,
                        "capture_profile": {
                            "type": "object",
                            "required": [
                                "board_name", "board_header", "gps_protocol", "mic_kind",
                                "mic_count", "fs_nominal_hz", "decimation", "fs_acquisition_hz",
                                "mic_band_lo_hz", "mic_band_hi_hz",
                            ],
                            "properties": {
                                "board_name": {"type": "string"},
                                "board_header": {"type": "string"},
                                "gps_protocol": {"type": "string"},
                                "mic_kind": {"type": "string"},
                                "mic_count": {"type": "integer"},
                                "fs_nominal_hz": {"type": "integer"},
                                "decimation": {"type": "integer"},
                                "fs_acquisition_hz": {"type": "integer"},
                                "mic_band_lo_hz": {"type": "integer"},
                                "mic_band_hi_hz": {"type": "integer"},
                            },
                            "additionalProperties": True,
                        },
                        "partition_table": {
                            "type": "object",
                            "required": [
                                "artifact", "sha256", "bytes", "source_path", "source_sha256",
                                "source_status",
                            ],
                            "properties": {
                                "artifact": {"type": "string"},
                                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                "bytes": {"type": "integer"},
                                "source_path": {"type": ["string", "null"]},
                                "source_sha256": {"type": ["string", "null"]},
                                "source_status": {"type": "string"},
                            },
                            "additionalProperties": True,
                        },
                        "artifacts": {"type": "array", "items": named_file_record},
                    },
                    "additionalProperties": True,
                },
            },
            "release_artifacts": {"type": "array", "items": named_file_record},
        },
        "additionalProperties": True,
    }


def _cmd_generate(args):
    manifest = write_manifest(
        pathlib.Path(args.repo_root).resolve(),
        pathlib.Path(args.dist).resolve(),
        pathlib.Path(args.build_info).resolve(),
        allow_dirty=args.allow_dirty,
    )
    print("release-manifest: wrote %s for %s (%s)"
          % (MANIFEST_NAME, manifest["tag"], ", ".join(v["board_class"] for v in manifest["variants"])))
    return 0


def _cmd_verify(args):
    dist_dir = pathlib.Path(args.dist).resolve()
    manifest_path = pathlib.Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = dist_dir / manifest_path
    summary = verify_release_directory(
        manifest_path.resolve(),
        dist_dir,
        expected_tag=args.tag,
        expected_board_class=args.board_class,
        expected_psram_mode=args.psram_mode,
        source_root=pathlib.Path(args.source_root).resolve() if args.source_root else None,
    )
    print("release-manifest: verified %s (%d artefacts%s)"
          % (summary["tag"], summary["artifacts_checked"],
             ", source checked" if summary["source_checked"] else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="write %s and %s into a release directory"
                                      % (MANIFEST_NAME, SCHEMA_NAME))
    gen.add_argument("--dist", required=True)
    gen.add_argument("--build-info", required=True)
    gen.add_argument("--repo-root", default=str(REPO))
    gen.add_argument("--allow-dirty", action="store_true",
                     help="write an explicitly unverifiable manifest instead of refusing a dirty tree")
    gen.set_defaults(func=_cmd_generate)

    verify = sub.add_parser("verify", help="verify a release directory offline")
    verify.add_argument("--manifest", default=MANIFEST_NAME)
    verify.add_argument("--dist", required=True)
    verify.add_argument("--tag")
    verify.add_argument("--board-class")
    verify.add_argument("--psram-mode", choices=board_profiles.known_psram_modes())
    verify.add_argument("--source-root")
    verify.set_defaults(func=_cmd_verify)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as e:
        die("release-manifest: %s" % e)


if __name__ == "__main__":
    raise SystemExit(main())
