#!/usr/bin/env python3
"""Generate and verify the hear_node device-push CA bundle release artifact, offline.

ADR 0011 deliberately keeps device TLS trust small and shared: the live single-record push path
and the future `/v1/ingest/batches` client both ride `setCACert(HEAR_PUSH_CA_CERT)`, and the
trust input is a bounded PEM bundle rather than a second store or a broad public CA set.

This tool is the release seam for that decision:

  * it assembles the bundle from tracked PEM source files listed in a tracked spec;
  * it refuses missing, extra, malformed or scope-breaking certificates instead of guessing;
  * it proves the checked-in default `hear_push_ca.h` macro still carries exactly that bundle, so
    the release artifact and the firmware trust input cannot silently drift apart.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import pathlib
import re
import ssl

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
TRUST_STORE = HERE / "trust_store"
SPEC_PATH = TRUST_STORE / "hear_push_ca_bundle_spec.json"
HEADER_PATH = HERE / "hear_push_ca.h"
BUNDLE_NAME = "hear-push-ca-bundle.pem"
METADATA_NAME = "hear-push-ca-bundle.json"
SCHEMA_VERSION = 1
SPEC_TYPE = "dama-hear-device-push-ca-bundle-spec"
METADATA_TYPE = "dama-hear-device-push-ca-bundle"
TARGET_MACRO = "HEAR_PUSH_CA_CERT"
EXPECTED_ADR = "docs/decisions/0011-device-push-trust-store-cutover-policy.md"
CERT_BLOCK_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+.*?\s+-----END CERTIFICATE-----\s*",
    re.S,
)
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def _json_text(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError("%s is not valid JSON: %s" % (path, e)) from e


def _pem_blocks(text: str, source: str):
    blocks = [m.group(0).strip() + "\n" for m in CERT_BLOCK_RE.finditer(text)]
    if not blocks:
        raise ValueError("%s contains no PEM certificate" % source)
    if CERT_BLOCK_RE.sub("", text).strip():
        raise ValueError("%s contains non-certificate text" % source)
    return blocks


def _checked_bundle_parse(bundle_text: str, source: str):
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(cadata=bundle_text)
    except ssl.SSLError as e:
        raise ValueError("%s is not a valid PEM CA bundle: %s" % (source, e)) from e


def _checked_cert(path: pathlib.Path):
    blocks = _pem_blocks(path.read_text(encoding="utf-8"), path.as_posix())
    if len(blocks) != 1:
        raise ValueError("%s must contain exactly one certificate, found %d"
                         % (path.as_posix(), len(blocks)))
    pem = blocks[0]
    try:
        der = ssl.PEM_cert_to_DER_cert(pem)
    except ValueError as e:
        raise ValueError("%s is not a valid PEM certificate: %s" % (path.as_posix(), e)) from e
    return pem, der


def load_bundle_spec(path: pathlib.Path = SPEC_PATH):
    spec = _load_json(path)
    required = {
        "schema_version", "bundle_type", "adr", "target_macro", "state",
        "steady_state_max_certificates", "overlap_max_certificates", "applies_to", "certificates",
    }
    missing = sorted(required - set(spec))
    if missing:
        raise ValueError("%s is missing %s" % (path.as_posix(), ", ".join(missing)))
    if spec["schema_version"] != SCHEMA_VERSION:
        raise ValueError("%s has unsupported schema_version %r"
                         % (path.as_posix(), spec["schema_version"]))
    if spec["bundle_type"] != SPEC_TYPE:
        raise ValueError("%s has unexpected bundle_type %r"
                         % (path.as_posix(), spec["bundle_type"]))
    if spec["adr"] != EXPECTED_ADR:
        raise ValueError("%s must point at %s, not %r"
                         % (path.as_posix(), EXPECTED_ADR, spec["adr"]))
    if spec["target_macro"] != TARGET_MACRO:
        raise ValueError("%s must target %s, not %r"
                         % (path.as_posix(), TARGET_MACRO, spec["target_macro"]))
    if spec["steady_state_max_certificates"] != 1:
        raise ValueError("%s must keep steady_state_max_certificates at 1 per ADR 0011"
                         % path.as_posix())
    if spec["overlap_max_certificates"] != 2:
        raise ValueError("%s must keep overlap_max_certificates at 2 per ADR 0011"
                         % path.as_posix())
    state = spec["state"]
    if state not in ("steady-state", "overlap"):
        raise ValueError("%s has unsupported state %r" % (path.as_posix(), state))
    certs = spec["certificates"]
    if not isinstance(certs, list) or not certs:
        raise ValueError("%s must declare at least one certificate" % path.as_posix())
    expected_count = 1 if state == "steady-state" else 2
    if len(certs) != expected_count:
        raise ValueError("%s declares state %r but lists %d certificates"
                         % (path.as_posix(), state, len(certs)))
    if len(certs) > spec["overlap_max_certificates"]:
        raise ValueError("%s lists %d certificates; ADR 0011 bounds overlap at %d"
                         % (path.as_posix(), len(certs), spec["overlap_max_certificates"]))
    applies_to = spec["applies_to"]
    if (not isinstance(applies_to, list) or len(applies_to) != 2
            or not all(isinstance(item, str) and item.strip() for item in applies_to)):
        raise ValueError("%s must declare exactly two non-empty applies_to entries"
                         % path.as_posix())
    return spec


def build_ca_bundle(repo_root: pathlib.Path, spec_path: pathlib.Path = SPEC_PATH):
    spec = load_bundle_spec(spec_path)
    certs = []
    bundle_parts = []
    seen_paths = set()
    for item in spec["certificates"]:
        for name in ("name", "role", "path", "sha256"):
            if name not in item or not str(item[name]).strip():
                raise ValueError("%s certificate entry is missing %s"
                                 % (spec_path.as_posix(), name))
        rel = item["path"]
        if rel in seen_paths:
            raise ValueError("%s repeats certificate path %s" % (spec_path.as_posix(), rel))
        seen_paths.add(rel)
        path = repo_root / rel
        if not path.exists():
            raise ValueError("certificate source %s is missing" % rel)
        pem, der = _checked_cert(path)
        got = _sha256_bytes(der)
        if got != item["sha256"]:
            raise ValueError("%s sha256 %s does not match spec %s"
                             % (rel, got, item["sha256"]))
        bundle_parts.append(pem)
        certs.append({
            "name": item["name"],
            "role": item["role"],
            "path": rel,
            "bytes": len(pem.encode("utf-8")),
            "sha256": got,
            "pem_sha256": _sha256_bytes(pem.encode("utf-8")),
        })
    bundle_text = "".join(bundle_parts)
    _checked_bundle_parse(bundle_text, BUNDLE_NAME)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "bundle_type": METADATA_TYPE,
        "name": BUNDLE_NAME,
        "adr": spec["adr"],
        "target_macro": spec["target_macro"],
        "state": spec["state"],
        "steady_state_max_certificates": spec["steady_state_max_certificates"],
        "overlap_max_certificates": spec["overlap_max_certificates"],
        "generated_from": spec_path.relative_to(repo_root).as_posix(),
        "bytes": len(bundle_text.encode("utf-8")),
        "sha256": _sha256_bytes(bundle_text.encode("utf-8")),
        "certificate_count": len(certs),
        "applies_to": list(spec["applies_to"]),
        "certificates": certs,
    }
    return bundle_text, metadata


def _header_bundle_text(path: pathlib.Path = HEADER_PATH):
    text = path.read_text(encoding="utf-8")
    m = re.search(r"#define\s+%s\s*\\\n(?P<body>.*?)(?=\n#endif)" % TARGET_MACRO, text, re.S)
    if not m:
        raise ValueError("%s does not define %s" % (path.as_posix(), TARGET_MACRO))
    parts = STRING_RE.findall(m.group("body"))
    if not parts:
        raise ValueError("%s does not contain a string literal for %s"
                         % (path.as_posix(), TARGET_MACRO))
    return "".join(ast.literal_eval(part) for part in parts)


def verify_header_matches_bundle(bundle_text: str, header_path: pathlib.Path = HEADER_PATH):
    header_bundle = _header_bundle_text(header_path)
    if header_bundle != bundle_text:
        raise ValueError("%s does not match the tracked %s bundle"
                         % (header_path.as_posix(), BUNDLE_NAME))


def write_release_artifacts(repo_root: pathlib.Path, dist_dir: pathlib.Path,
                            spec_path: pathlib.Path = SPEC_PATH,
                            header_path: pathlib.Path = HEADER_PATH):
    bundle_text, metadata = build_ca_bundle(repo_root, spec_path)
    verify_header_matches_bundle(bundle_text, header_path)
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / BUNDLE_NAME).write_text(bundle_text, encoding="utf-8")
    (dist_dir / METADATA_NAME).write_text(_json_text(metadata), encoding="utf-8")
    return metadata


def verify_release_artifacts(repo_root: pathlib.Path, dist_dir: pathlib.Path,
                             spec_path: pathlib.Path = SPEC_PATH,
                             header_path: pathlib.Path = HEADER_PATH):
    bundle_path = dist_dir / BUNDLE_NAME
    metadata_path = dist_dir / METADATA_NAME
    if not bundle_path.exists():
        raise ValueError("%s is missing from %s" % (BUNDLE_NAME, dist_dir))
    if not metadata_path.exists():
        raise ValueError("%s is missing from %s" % (METADATA_NAME, dist_dir))
    expected_bundle, expected_metadata = build_ca_bundle(repo_root, spec_path)
    verify_header_matches_bundle(expected_bundle, header_path)
    bundle_text = bundle_path.read_text(encoding="utf-8")
    _checked_bundle_parse(bundle_text, bundle_path.name)
    if bundle_text != expected_bundle:
        raise ValueError("%s does not match the tracked certificate bundle" % bundle_path.name)
    metadata = _load_json(metadata_path)
    if metadata != expected_metadata:
        raise ValueError("%s does not match the tracked bundle metadata" % metadata_path.name)
    return expected_metadata


def _cmd_generate(args):
    meta = write_release_artifacts(
        pathlib.Path(args.repo_root).resolve(),
        pathlib.Path(args.dist).resolve(),
        pathlib.Path(args.spec).resolve(),
        pathlib.Path(args.header).resolve(),
    )
    print("%s: %d cert(s), %s" % (BUNDLE_NAME, meta["certificate_count"], meta["state"]))


def _cmd_verify(args):
    meta = verify_release_artifacts(
        pathlib.Path(args.repo_root).resolve(),
        pathlib.Path(args.dist).resolve(),
        pathlib.Path(args.spec).resolve(),
        pathlib.Path(args.header).resolve(),
    )
    print("verified %s (%d certs, sha256 %s)"
          % (BUNDLE_NAME, meta["certificate_count"], meta["sha256"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="write the CA bundle release artifacts into a dist dir")
    gen.add_argument("--dist", required=True)
    gen.add_argument("--repo-root", default=str(REPO))
    gen.add_argument("--spec", default=str(SPEC_PATH))
    gen.add_argument("--header", default=str(HEADER_PATH))
    gen.set_defaults(func=_cmd_generate)

    verify = sub.add_parser("verify", help="verify CA bundle artifacts against tracked sources")
    verify.add_argument("--dist", required=True)
    verify.add_argument("--repo-root", default=str(REPO))
    verify.add_argument("--spec", default=str(SPEC_PATH))
    verify.add_argument("--header", default=str(HEADER_PATH))
    verify.set_defaults(func=_cmd_verify)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
