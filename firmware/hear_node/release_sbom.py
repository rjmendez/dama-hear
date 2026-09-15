#!/usr/bin/env python3
"""Generate and verify the hear_node release SBOM (CycloneDX 1.6 JSON), offline.

WHY A FIRMWARE SBOM LOOKS LIKE THIS. There is no package manager in this build. `arduino-cli`
compiles a sketch against a pinned esp32 board package and the libraries vendored in
`firmware/lib`, and emits five binaries per variant. So the answer to "what is in this image" is
not a dependency resolution; it is:

  * the exact source closure the manifest already records (sketch, board headers, generated
    headers and their generators, installer and workflow files), by sha256;
  * the vendored libraries under `firmware/lib`, each with a digest over its own file tree;
  * the two toolchain versions that decide what those sources compile into -- `arduino-cli` and
    the esp32 Arduino core -- plus the FQBN and build flags per variant;
  * the published binaries themselves, by sha256.

⚠️IT IS DERIVED, NOT ASSERTED. Every component here is computed from the same tree and the same
dist directory the release manifest is computed from, by the same code path, so an SBOM that
disagrees with the manifest is a bug that `verify` fails on rather than a claim nobody checks.

⚠️IT IS DETERMINISTIC. No timestamp, no random serial number: the serial number is a UUIDv5 of
(tag, commit), so two runs on the same commit produce byte-identical SBOMs and the manifest can
hash it.

⚠️WHAT IT DOES NOT COVER, stated in the document itself (`scope.excludes`): the contents of the
esp32 board package -- its ESP-IDF fork, its toolchain binaries and the second-stage bootloader
it ships -- are named by version, not enumerated. The release does not build them and cannot
attest to their contents; the version pin plus the published binary digest is what is provable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import uuid

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import release_manifest as rm  # noqa: E402

SBOM_NAME = rm.SBOM_NAME
SBOM_FORMAT = "CycloneDX"
SBOM_SPEC_VERSION = "1.6"
SBOM_KIND = rm.SBOM_KIND
VENDORED_LIB_ROOT = "firmware/lib"
NAMESPACE = uuid.UUID("6b1f4b3e-6f9a-5a2b-9a3d-0e5b5b7a1d21")
SCOPE = {
    "covers": [
        "published-firmware-binaries",
        "source-closure",
        "vendored-libraries",
        "toolchain-versions",
    ],
    "excludes": [
        "esp32 board package contents (ESP-IDF fork, gcc toolchain, second-stage bootloader): "
        "pinned by version, not enumerated -- this release does not build them",
        "runtime data a node acquires or is provisioned with (NVS record, Wi-Fi, tokens): never "
        "part of a published image",
        "host tooling used by an operator (python, gh, arduino-cli plugins) outside the build",
    ],
    "derivation": "computed from the release tree and dist directory, offline; hashes are sha256",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_text(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _hash_ref(digest: str):
    return [{"alg": "SHA-256", "content": digest}]


def _tree_digest(root: pathlib.Path):
    """A digest over a directory tree: sha256 of "relpath sha256" lines, sorted.

    A vendored library has no version to pin, so its identity is its content. Sorted relative
    paths mean the digest does not depend on the filesystem's directory order.
    """
    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        lines.append("%s %s" % (rel, _sha256_bytes(path.read_bytes())))
    return _sha256_bytes("\n".join(lines).encode("utf-8")), len(lines)


def _vendored_libraries(repo_root: pathlib.Path):
    lib_root = repo_root / VENDORED_LIB_ROOT
    out = []
    if not lib_root.is_dir():
        return out
    for path in sorted(p for p in lib_root.iterdir() if p.is_dir()):
        digest, files = _tree_digest(path)
        rel = path.relative_to(repo_root).as_posix()
        out.append({
            "type": "library",
            "bom-ref": "lib/%s" % rel,
            "name": path.name,
            "scope": "required",
            "hashes": _hash_ref(digest),
            "properties": [
                {"name": "dama-hear:source-path", "value": rel},
                {"name": "dama-hear:vendored", "value": "true"},
                {"name": "dama-hear:file-count", "value": str(files)},
                {"name": "dama-hear:digest-kind", "value": "sha256-of-sorted-path-digest-lines"},
            ],
        })
    return out


def _source_components(manifest):
    out = []
    for section, scope in (("inputs", "source"),
                           ("generated_files", "generated"),
                           ("schema_guards", "schema-guard")):
        for item in manifest.get(section, []):
            props = [
                {"name": "dama-hear:source-path", "value": item["path"]},
                {"name": "dama-hear:closure", "value": scope},
            ]
            if item.get("role"):
                props.append({"name": "dama-hear:role", "value": item["role"]})
            if item.get("generator"):
                props.append({"name": "dama-hear:generator", "value": item["generator"]})
                props.append({"name": "dama-hear:generator-sha256",
                              "value": item["generator_sha256"]})
            out.append({
                "type": "file",
                "bom-ref": "src/%s" % item["path"],
                "name": item["path"],
                "hashes": _hash_ref(item["sha256"]),
                "properties": props,
            })
    return out


def _artifact_components(manifest):
    out = []
    for variant in manifest.get("variants", []):
        flags = " ".join(variant.get("build_flags") or [])
        for item in variant.get("artifacts", []):
            out.append({
                "type": "file",
                "bom-ref": "asset/%s" % item["name"],
                "name": item["name"],
                "version": manifest["tag"],
                "hashes": _hash_ref(item["sha256"]),
                "properties": [
                    {"name": "dama-hear:artifact-kind", "value": item.get("kind", "")},
                    {"name": "dama-hear:board-class", "value": variant["board_class"]},
                    {"name": "dama-hear:psram-mode", "value": variant["psram_mode"]},
                    {"name": "dama-hear:release-stem", "value": variant["release_stem"]},
                    {"name": "dama-hear:fqbn", "value": variant.get("fqbn") or ""},
                    {"name": "dama-hear:build-flags", "value": flags},
                    {"name": "dama-hear:image-class",
                     "value": manifest["build"].get("image_class", "")},
                    {"name": "dama-hear:compiled-in-credentials",
                     "value": "false" if not manifest["build"].get("compiled_in_credentials")
                              else "true"},
                ],
            })
    for item in manifest.get("release_artifacts", []):
        if item.get("kind") == SBOM_KIND:
            continue                    # a document does not describe itself
        out.append({
            "type": "file",
            "bom-ref": "asset/%s" % item["name"],
            "name": item["name"],
            "version": manifest["tag"],
            "hashes": _hash_ref(item["sha256"]),
            "properties": [{"name": "dama-hear:artifact-kind", "value": item.get("kind", "")}],
        })
    return out


def _toolchain_components(manifest):
    build = manifest["build"]
    return [
        {
            "type": "application",
            "bom-ref": "tool/arduino-cli",
            "name": "arduino-cli",
            "version": build["arduino_cli_version"],
            "scope": "excluded",
            "properties": [{"name": "dama-hear:role", "value": "build-driver"}],
        },
        {
            "type": "framework",
            "bom-ref": "tool/esp32-arduino-core",
            "name": "esp32:esp32",
            "version": build["esp32_core_version"],
            "scope": "required",
            "externalReferences": [{
                "type": "distribution",
                "url": "https://espressif.github.io/arduino-esp32/package_esp32_index.json",
            }],
            "properties": [
                {"name": "dama-hear:role", "value": "board-package"},
                {"name": "dama-hear:default-fqbn", "value": build["fqbn"]},
                {"name": "dama-hear:contents-enumerated", "value": "false"},
            ],
        },
    ]


def build_sbom(repo_root: pathlib.Path, dist_dir: pathlib.Path, build_info_path: pathlib.Path,
               allow_dirty=False, manifest=None):
    """The SBOM for one release, derived from the same closure as the release manifest."""
    if manifest is None:
        manifest, _ = rm.build_manifest(repo_root, dist_dir, build_info_path,
                                        allow_dirty=allow_dirty)
    source = manifest["source"]
    tag = manifest["tag"]
    commit = source["commit"]
    serial = uuid.uuid5(NAMESPACE, "%s/%s/%s" % (source["repository"], tag, commit))

    root = {
        "type": "firmware",
        "bom-ref": "hear_node@%s" % tag,
        "name": "hear_node",
        "version": tag,
        "description": "dama-hear acoustic node firmware, unprovisioned release image set",
        "externalReferences": [
            {"type": "vcs", "url": "https://github.com/%s" % source["repository"]},
            {"type": "build-system",
             "url": "https://github.com/%s/actions/workflows/release.yml" % source["repository"]},
        ],
        "properties": [
            {"name": "dama-hear:source-commit", "value": commit},
            {"name": "dama-hear:source-describe", "value": source["describe"]},
            {"name": "dama-hear:source-dirty", "value": "true" if source["dirty"] else "false"},
            {"name": "dama-hear:sketch", "value": manifest["build"]["sketch"]},
            {"name": "dama-hear:image-class",
             "value": manifest["build"].get("image_class", "")},
        ],
    }

    components = (_toolchain_components(manifest)
                  + _vendored_libraries(repo_root)
                  + _source_components(manifest)
                  + _artifact_components(manifest))
    depends_on = sorted(c["bom-ref"] for c in components)
    sbom = {
        "bomFormat": SBOM_FORMAT,
        "specVersion": SBOM_SPEC_VERSION,
        "serialNumber": "urn:uuid:%s" % serial,
        "version": 1,
        "metadata": {
            "component": root,
            "tools": {"components": [{
                "type": "application",
                "name": "release_sbom.py",
                "version": str(rm.SCHEMA_VERSION),
                "publisher": source["repository"],
            }]},
            "properties": [
                {"name": "dama-hear:sbom-scope", "value": json.dumps(SCOPE, sort_keys=True)},
                {"name": "dama-hear:manifest", "value": rm.MANIFEST_NAME},
                {"name": "dama-hear:release-tag", "value": tag},
            ],
        },
        "components": components,
        "dependencies": [{"ref": root["bom-ref"], "dependsOn": depends_on}],
    }
    return sbom


def sbom_bytes(sbom) -> bytes:
    return _json_text(sbom).encode("utf-8")


def write_sbom(repo_root: pathlib.Path, dist_dir: pathlib.Path, build_info_path: pathlib.Path,
               allow_dirty=False):
    sbom = build_sbom(repo_root, dist_dir, build_info_path, allow_dirty=allow_dirty)
    data = sbom_bytes(sbom)
    (dist_dir / SBOM_NAME).write_bytes(data)
    return sbom


def component_index(sbom):
    """{component name: sha256} for every component that carries a SHA-256 hash."""
    out = {}
    for comp in sbom.get("components", []):
        for h in comp.get("hashes", []):
            if h.get("alg") == "SHA-256":
                out[comp.get("name")] = h.get("content")
    return out


def sbom_problems(sbom, manifest=None, dist_dir: pathlib.Path | None = None):
    """Everything wrong with an SBOM document, as plain strings. Offline; no network, no clock."""
    problems = []
    if not isinstance(sbom, dict):
        return ["SBOM must be a JSON object, got %s" % type(sbom).__name__]
    if sbom.get("bomFormat") != SBOM_FORMAT:
        problems.append("SBOM bomFormat is %r, expected %r" % (sbom.get("bomFormat"), SBOM_FORMAT))
    if sbom.get("specVersion") != SBOM_SPEC_VERSION:
        problems.append("SBOM specVersion is %r, expected %r"
                        % (sbom.get("specVersion"), SBOM_SPEC_VERSION))
    serial = sbom.get("serialNumber")
    if not isinstance(serial, str) or not serial.startswith("urn:uuid:"):
        problems.append("SBOM serialNumber %r is not a urn:uuid" % serial)
    root = (sbom.get("metadata") or {}).get("component") or {}
    if root.get("name") != "hear_node":
        problems.append("SBOM root component is %r, expected 'hear_node'" % root.get("name"))
    if not sbom.get("components"):
        problems.append("SBOM declares no components")
    index = component_index(sbom)
    for name, digest in index.items():
        if not isinstance(digest, str) or len(digest) != 64:
            problems.append("SBOM component %r has a malformed sha256 %r" % (name, digest))

    if manifest is not None:
        tag = manifest.get("tag")
        if root.get("version") != tag:
            problems.append("SBOM is for %r, manifest is for %r" % (root.get("version"), tag))
        props = {p.get("name"): p.get("value") for p in root.get("properties", [])}
        want_commit = (manifest.get("source") or {}).get("commit")
        if props.get("dama-hear:source-commit") != want_commit:
            problems.append("SBOM source commit %r does not match manifest %r"
                            % (props.get("dama-hear:source-commit"), want_commit))
        want = {}
        for variant in manifest.get("variants", []):
            for item in variant.get("artifacts", []):
                want[item["name"]] = item["sha256"]
        for item in manifest.get("release_artifacts", []):
            if item.get("kind") != SBOM_KIND:
                want[item["name"]] = item["sha256"]
        for section in ("inputs", "generated_files", "schema_guards"):
            for item in manifest.get(section, []):
                want[item["path"]] = item["sha256"]
        for name, digest in sorted(want.items()):
            got = index.get(name)
            if got is None:
                problems.append("SBOM does not describe %s, which the manifest publishes" % name)
            elif got != digest:
                problems.append("SBOM sha256 for %s is %s, manifest says %s" % (name, got, digest))
        build = manifest.get("build") or {}
        tools = {c.get("name"): c.get("version")
                 for c in _toolchain_components({"build": build}) if build}
        for comp in sbom.get("components", []):
            if comp.get("name") in tools and comp.get("version") != tools[comp["name"]]:
                problems.append("SBOM says %s %s, manifest says %s"
                                % (comp["name"], comp.get("version"), tools[comp["name"]]))

    if dist_dir is not None:
        for comp in sbom.get("components", []):
            name = comp.get("name")
            if not str(comp.get("bom-ref", "")).startswith("asset/"):
                continue
            path = dist_dir / name
            if not path.exists():
                problems.append("SBOM describes %s, which is not in the release directory" % name)
                continue
            got = _sha256_bytes(path.read_bytes())
            if got != index.get(name):
                problems.append("%s sha256 %s, SBOM says %s" % (name, got, index.get(name)))
    return problems


def verify_sbom(sbom, manifest=None, dist_dir: pathlib.Path | None = None):
    problems = sbom_problems(sbom, manifest=manifest, dist_dir=dist_dir)
    if problems:
        shown, extra = problems[:8], len(problems) - 8
        raise ValueError("release SBOM does not describe this release: "
                         + "; ".join(shown) + (" (+%d more)" % extra if extra > 0 else ""))
    return {
        "tag": (sbom.get("metadata") or {}).get("component", {}).get("version"),
        "components": len(sbom.get("components", [])),
    }


def load_sbom(source):
    if isinstance(source, dict):
        return source
    if isinstance(source, pathlib.Path):
        source = source.read_bytes()
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    try:
        return json.loads(source)
    except json.JSONDecodeError as e:
        raise ValueError("release SBOM is not valid JSON: %s" % e) from e


def _cmd_generate(args):
    sbom = write_sbom(
        pathlib.Path(args.repo_root).resolve(),
        pathlib.Path(args.dist).resolve(),
        pathlib.Path(args.build_info).resolve(),
        allow_dirty=args.allow_dirty,
    )
    print("release-sbom: wrote %s (%s %s, %d components)"
          % (SBOM_NAME, SBOM_FORMAT, SBOM_SPEC_VERSION, len(sbom["components"])))
    return 0


def _cmd_verify(args):
    dist = pathlib.Path(args.dist).resolve()
    sbom = load_sbom(dist / args.sbom)
    manifest = None
    manifest_path = dist / args.manifest
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = verify_sbom(sbom, manifest=manifest, dist_dir=dist)
    if args.tag and summary["tag"] != args.tag:
        raise ValueError("SBOM is for %r, not %r" % (summary["tag"], args.tag))
    print("release-sbom: verified %s for %s (%d components%s)"
          % (SBOM_NAME, summary["tag"], summary["components"],
             ", cross-checked against " + rm.MANIFEST_NAME if manifest else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="write %s into a release directory" % SBOM_NAME)
    gen.add_argument("--dist", required=True)
    gen.add_argument("--build-info", required=True)
    gen.add_argument("--repo-root", default=str(REPO))
    gen.add_argument("--allow-dirty", action="store_true")
    gen.set_defaults(func=_cmd_generate)

    ver = sub.add_parser("verify", help="verify a release SBOM offline")
    ver.add_argument("--dist", required=True)
    ver.add_argument("--sbom", default=SBOM_NAME)
    ver.add_argument("--manifest", default=rm.MANIFEST_NAME)
    ver.add_argument("--tag")
    ver.set_defaults(func=_cmd_verify)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as e:
        raise SystemExit("release-sbom: %s" % e)


if __name__ == "__main__":
    raise SystemExit(main())
