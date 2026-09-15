#!/usr/bin/env python3
"""Reproducible Phase 0 contract freeze for checked-in dama-hear interfaces.

This tool inventories only repository-visible contracts. It deliberately does NOT read live
cluster state, Redis contents, secrets, or private corpus payloads.

    python3 tools/freeze_contracts.py --format json
    python3 tools/freeze_contracts.py --format markdown
    python3 tools/freeze_contracts.py --check
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firmware.hear_node import board_profiles as BP  # noqa: E402
from hear import detsfile as DF  # noqa: E402
from hear import pool as HP  # noqa: E402
from hear import scenefile as SF  # noqa: E402
from hear import wire as WR  # noqa: E402
from tools import hear_heartbeat_receiver as HR  # noqa: E402
from tools import hear_mqtt_bridge as HMB  # noqa: E402

VERSION = 1
JSON_OUTPUT = ROOT / "docs" / "data" / "phase0-freeze-contracts.v1.json"
MARKDOWN_OUTPUT = ROOT / "docs" / "phase0-freeze-contracts.v1.md"

WIRE_SOURCE_FILES = (
    "hear/wire.py",
    "hear/sketch.py",
    "firmware/hear_node/mel_impulse.h",
    "firmware/hear_node/sketch_domain.h",
    "testdata/sketch_golden.json",
)

FIRMWARE_METADATA_FILES = (
    ".github/workflows/firmware.yml",
    ".github/workflows/release.yml",
    "firmware/README.md",
    "firmware/hear_node/README.md",
    "firmware/hear_node/board_profiles.py",
    "firmware/hear_node/hear_node.ino",
    "firmware/hear_node/hear_push_payload.h",
    "firmware/hear_node/mel_impulse.h",
    "firmware/hear_node/mel_scene.h",
    "firmware/hear_node/gen_secrets.py",
    "firmware/puc_node/partitions.csv",
    "requirements/ci-dev.txt",
    "requirements/ci-pods.txt",
)

SAFE_FIXTURE_ROOTS = (
    ROOT / "docs" / "data",
    ROOT / "testdata",
    ROOT / "tests" / "fixtures",
)
FIXTURE_EXCLUDES = {
    "docs/data/phase0-freeze-contracts.v1.json",
}

HEARTBEAT_FIELDS = [
    "telemetry_path",
    "telemetry_schema_version",
    "device_id",
    "ts",
    "ts_ms",
    "class",
    "fw_version",
    "uptime_s",
    "gps.fix",
    "time.valid",
    "wifi.rssi_dbm",
    "counters.scene_rows_written",
    "counters.dets_rows_written",
    "counters.clips_written",
    "counters.clips_evicted",
]

EVENT_FIELDS = [
    "telemetry_path",
    "telemetry_schema_version",
    "device_id",
    "ts",
    "ts_ms",
    "class",
    "fw_version",
    "uptime_s",
    "time.valid",
    "event_type",
    "event_seq",
    "event.clip_basename",
    "event.clips_written",
    "event.clips_evicted",
    "event.dets_rows_written",
    "event.batch_rows",
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def object_hash(data: Any) -> str:
    return sha256_bytes(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def with_hash(section: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(section)
    out["section_hash"] = object_hash(section)
    return out


def relpath(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def file_record(rel: str) -> Dict[str, Any]:
    path = ROOT / rel
    raw = path.read_bytes()
    return {"path": rel, "size_bytes": len(raw), "sha256": sha256_bytes(raw)}


def file_records(paths: Iterable[str]) -> List[Dict[str, Any]]:
    return [file_record(p) for p in sorted(paths)]


def _env_lookup_key(call: ast.Call) -> str | None:
    """Return the env var key read by os.environ.get(KEY, ...) / os.getenv(KEY, ...)."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr == "get":
        target = func.value
        is_environ = (
            (isinstance(target, ast.Attribute) and target.attr == "environ")
            or (isinstance(target, ast.Name) and target.id == "environ")
        )
        if not is_environ:
            return None
    elif func.attr != "getenv":
        return None
    if len(call.args) < 2:
        return None
    key = call.args[0]
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    return None


def _unwrap_call(node: ast.AST) -> ast.AST:
    """Strip simple coercion wrappers such as int(...)/float(...)/str(...)."""
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"int", "float", "str", "bool"}
        and len(node.args) == 1
    ):
        node = node.args[0]
    return node


def extract_default_string(rel: str, env_key: str, fallback: str | None = None) -> str:
    """Resolve the literal default bound by an env lookup, regardless of the assigned name.

    The assignment target (e.g. ``DEFAULT_MQTT_TOPIC``) is usually not the env var key, so
    the value is located by matching the ``os.environ.get(env_key, default)`` call itself.
    A ``fallback`` is only honoured when no such assignment exists; otherwise the real
    source default is returned so contract hashes track source changes.
    """
    path = ROOT / rel
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
        else:
            continue
        value = _unwrap_call(value)
        if not isinstance(value, ast.Call) or _env_lookup_key(value) != env_key:
            continue
        default = _unwrap_call(value.args[1])
        if isinstance(default, ast.Constant) and default.value is not None:
            return str(default.value)
    if fallback is None:
        raise ValueError(
            "no literal default found for env key %r in %s" % (env_key, rel)
        )
    return fallback


def wire_profiles() -> Dict[str, Any]:
    profiles = []
    for profile_id, geom in sorted(WR.PROFILE_GEOMETRY.items()):
        row = {
            "profile_id": profile_id,
            "bands": geom.bands,
            "frames": geom.frames,
            "nfft": geom.nfft,
            "hop_s": geom.hop_s,
            "fs_hz": geom.fs_hz,
            "layout": geom.layout,
            "f_lo_hz": geom.f_lo,
            "f_hi_hz": geom.f_hi,
            "wire_size_bytes": WR.wire_size_v2(profile_id),
            "fits_meshtastic_v2": WR.fits_meshtastic_v2(profile_id),
            "legacy_read_only": profile_id in WR.LEGACY_PROFILES,
        }
        row["profile_hash"] = object_hash(row)
        profiles.append(row)
    return with_hash({
        "default_profile_id": WR.DEFAULT_PROFILE,
        "meshtastic_usable_bytes": WR.MESHTASTIC_USABLE,
        "new_profile_ids": sorted(WR.NEW_PROFILE_IDS),
        "source_files": file_records(WIRE_SOURCE_FILES),
        "profiles": profiles,
    })


def firmware_build_metadata() -> Dict[str, Any]:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "firmware.yml").read_text())
    matrix = workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
    toolchain = {
        "arduino_cli_version": workflow.get("env", {}).get("ARDUINO_CLI_VERSION"),
        "esp32_core": workflow.get("env", {}).get("ESP32_CORE"),
        "esp32_index": workflow.get("env", {}).get("ESP32_INDEX"),
    }
    builds = []
    for entry in matrix:
        builds.append({
            "sketch": entry["sketch"],
            "fqbn": entry["fqbn"],
            "guarded": bool(entry["guarded"]),
            "board_class": entry.get("board_class") or None,
            "release_stem": entry.get("release_stem") or None,
            "artifact": entry["artifact"],
            "build_flags": [x for x in str(entry.get("build_flags", "")).split() if x],
            "guard_flags": [x for x in str(entry.get("guard_flags", "")).split() if x],
        })
    board_profiles = []
    for board_class, meta in sorted(BP.BOARD_PROFILES.items()):
        board_profiles.append({
            "board_class": board_class,
            "cpp_flags": list(meta["cpp_flags"]),
            "release_stem": meta["release_stem"],
            "upload_kinds": [
                {
                    "kind": kind,
                    "artifact_suffix": suffix,
                    "upload_filename": filename,
                }
                for kind, (suffix, filename) in sorted(BP.UPLOAD_SUFFIXES.items())
            ],
        })
    return with_hash({
        "toolchain": toolchain,
        "default_board_class": BP.DEFAULT_BOARD_CLASS,
        "fqbn": BP.FQBN,
        "workflow_builds": builds,
        "board_profiles": board_profiles,
        "source_files": file_records(FIRMWARE_METADATA_FILES),
    })


def schema_identifiers() -> List[Dict[str, Any]]:
    found: Dict[str, set[str]] = {}
    schema_key = re.compile(r'["\']schema["\']\s*:\s*["\']([A-Za-z0-9._-]+)["\']')
    schema_const = re.compile(r'\b[A-Z_]*SCHEMA[A-Z_]*\s*=\s*["\']([A-Za-z0-9._-]+)["\']')
    for root in ("hear", "modules", "tools", "testdata", "tests/fixtures", "docs/data"):
        base = ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in {".py", ".json"} or not path.is_file():
                continue
            if relpath(path) in FIXTURE_EXCLUDES:
                continue
            text = path.read_text(errors="replace")
            matches = set(schema_key.findall(text)) | set(schema_const.findall(text))
            for item in matches:
                found.setdefault(item, set()).add(relpath(path))
    return [
        {"schema": schema, "source_paths": sorted(paths)}
        for schema, paths in sorted(found.items())
    ]


def serialize_generation(name: str, declared: Iterable[str], written: Iterable[str]) -> Dict[str, Any]:
    row = {
        "generation": name,
        "declared_header": list(declared),
        "written_header": list(written),
        "broken_header": list(declared) != list(written),
    }
    row["header_hash"] = object_hash({
        "declared_header": row["declared_header"],
        "written_header": row["written_header"],
    })
    return row


def schemas() -> Dict[str, Any]:
    telemetry_routes = [
        {
            "http_path": "/api/hear/heartbeat",
            "telemetry_path": "hear/heartbeat",
            "required_fields": HEARTBEAT_FIELDS,
        },
        {
            "http_path": "/api/hear/event",
            "telemetry_path": "hear/event",
            "required_fields": EVENT_FIELDS,
            "event_types": sorted(HR._EVENT_TYPES),
        },
    ]
    for route in telemetry_routes:
        route["route_hash"] = object_hash(route)
    return with_hash({
        "pool_schema_version": HP.SCHEMA_VERSION,
        "receiver_schema_version": HR.RECEIVER_SCHEMA_VERSION,
        "telemetry_schema_version": 1,
        "bridge_telemetry_paths": sorted(HMB._ROUTES),
        "dets_csv": {
            "latest_generation": DF.LATEST.name,
            "frame_hex_lengths": sorted(DF.FRAME_HEX_LENS),
            "generations": [
                serialize_generation(g.name, g.declared, g.written)
                for g in DF.GENERATIONS
            ],
        },
        "scene_csv": {
            "latest_generation": SF.LATEST.name,
            "generations": [
                serialize_generation(g.name, g.declared, g.written)
                for g in SF.GENERATIONS
            ],
        },
        "telemetry_routes": telemetry_routes,
        "schema_identifiers": schema_identifiers(),
    })


def mqtt_topics() -> Dict[str, Any]:
    spatial_topic = extract_default_string("hear/spatial.py", "MQTT_SPATIAL_TOPIC")
    topics = [
        {
            "topic_pattern": "dama/+/acoustic_sketch",
            "role": "captured sketch ingest and replay lane",
            "source_paths": ["hear/corpus.py", "hear/pool.py", "tools/hear_score.py"],
        },
        {
            "topic_pattern": HMB.MQTT_TOPIC,
            "resolved_shape": "dama/<device_id>/telemetry",
            "role": "bridge subscription for hear_node telemetry relayed through MQTT",
            "source_paths": ["tools/hear_mqtt_bridge.py", "deploy/k8s/hear-mqtt-bridge.yaml"],
        },
        {
            "topic_pattern": spatial_topic,
            "role": "optional GeoJSON publish from the spatial pipeline",
            "source_paths": ["hear/spatial.py", "tools/hear_spatial.py"],
        },
    ]
    for topic in topics:
        topic["topic_hash"] = object_hash(topic)
    return with_hash({"topics": topics})


def redis_keys() -> Dict[str, Any]:
    keys = [
        {
            "pattern": "dama:hear:{device_id}",
            "kind": "string-with-ttl",
            "writer": "HeartbeatReceiverStore.write_heartbeat",
            "ttl_seconds": HR.HEARTBEAT_TTL_S,
            "source_paths": ["tools/hear_heartbeat_receiver.py"],
        },
        {
            "pattern": "dama:hear:devices",
            "kind": "set",
            "writer": "HeartbeatReceiverStore.write_heartbeat/write_event",
            "source_paths": ["tools/hear_heartbeat_receiver.py"],
        },
        {
            "pattern": "dama:hear:latest",
            "kind": "string",
            "writer": "HeartbeatReceiverStore.write_heartbeat",
            "source_paths": ["tools/hear_heartbeat_receiver.py"],
        },
        {
            "pattern": "dama:hear:event:{device_id}",
            "kind": "string",
            "writer": "HeartbeatReceiverStore.write_event",
            "source_paths": ["tools/hear_heartbeat_receiver.py"],
        },
        {
            "pattern": HR.EVENT_STREAM_KEY,
            "kind": "stream",
            "writer": "HeartbeatReceiverStore.write_event",
            "maxlen": HR.EVENT_STREAM_MAXLEN,
            "configurable_env": "HEAR_EVENT_STREAM_KEY",
            "source_paths": ["tools/hear_heartbeat_receiver.py"],
        },
    ]
    for key in keys:
        key["key_hash"] = object_hash(key)
    return with_hash({"keys": keys})


def env_binding(env: Mapping[str, Any]) -> Dict[str, Any]:
    if "valueFrom" in env:
        value_from = env["valueFrom"]
        if "secretKeyRef" in value_from:
            ref = value_from["secretKeyRef"]
            return {
                "name": env["name"],
                "binding": "secretKeyRef",
                "secret_name": ref.get("name"),
                "secret_key": ref.get("key"),
                "optional": bool(ref.get("optional", False)),
            }
        return {"name": env["name"], "binding": "valueFrom"}
    return {"name": env["name"], "binding": "literal"}


def k8s_layout() -> Dict[str, Any]:
    manifests = []
    objects = []
    pvc_index: Dict[str, Dict[str, Any]] = {}
    for path in sorted((ROOT / "deploy" / "k8s").glob("*.yaml")):
        if path.name.endswith("-code.yaml"):
            continue
        manifests.append(file_record(relpath(path)))
        text = path.read_text()
        path_refs = sorted(
            set(m.rstrip(".,;:") for m in re.findall(r"/pool(?:/[A-Za-z0-9._-]+)*", text))
        )
        for doc in (d for d in yaml.safe_load_all(text) if d):
            kind = doc.get("kind")
            meta = doc.get("metadata") or {}
            entry: Dict[str, Any] = {
                "manifest": relpath(path),
                "kind": kind,
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "path_references": path_refs,
            }
            spec = doc.get("spec") or {}
            pod_spec = None
            if kind == "PersistentVolumeClaim":
                entry["access_modes"] = list(spec.get("accessModes") or [])
                entry["storage_request"] = (
                    ((spec.get("resources") or {}).get("requests") or {}).get("storage")
                )
                pvc_index[meta.get("name")] = {
                    "claim_name": meta.get("name"),
                    "declared_in": relpath(path),
                    "access_modes": list(spec.get("accessModes") or []),
                    "storage_request": entry.get("storage_request"),
                    "consumers": [],
                    "path_references": [],
                }
            elif kind == "CronJob":
                entry["schedule"] = spec.get("schedule")
                pod_spec = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
                            ).get("spec")
            elif kind == "Deployment":
                pod_spec = ((spec.get("template") or {}).get("spec") or {})
            elif kind == "Service":
                entry["ports"] = [
                    {"name": p.get("name"), "port": p.get("port"), "targetPort": p.get("targetPort")}
                    for p in spec.get("ports") or []
                ]
            if pod_spec:
                entry["host_network"] = bool(pod_spec.get("hostNetwork", False))
                volumes = {v["name"]: v for v in pod_spec.get("volumes") or [] if "name" in v}
                containers = []
                for container in pod_spec.get("containers") or []:
                    mounts = []
                    for mount in container.get("volumeMounts") or []:
                        vol = volumes.get(mount.get("name"), {})
                        claim_name = ((vol.get("persistentVolumeClaim") or {}).get("claimName"))
                        mounts.append({
                            "volume_name": mount.get("name"),
                            "mount_path": mount.get("mountPath"),
                            "sub_path": mount.get("subPath"),
                            "claim_name": claim_name,
                        })
                        if claim_name:
                            pvc = pvc_index.setdefault(claim_name, {
                                "claim_name": claim_name,
                                "declared_in": None,
                                "access_modes": [],
                                "storage_request": None,
                                "consumers": [],
                                "path_references": [],
                            })
                            pvc["consumers"].append({
                                "manifest": relpath(path),
                                "kind": kind,
                                "object_name": meta.get("name"),
                                "container": container.get("name"),
                                "mount_path": mount.get("mountPath"),
                            })
                            pvc["path_references"] = sorted(set(pvc["path_references"]) | set(path_refs))
                    containers.append({
                        "name": container.get("name"),
                        "image": container.get("image"),
                        "env_bindings": [env_binding(e) for e in container.get("env") or [] if "name" in e],
                        "mounts": mounts,
                    })
                entry["containers"] = containers
            objects.append(entry)
    return with_hash({
        "manifest_files": manifests + [file_record("deploy/k8s/README.md")],
        "objects": objects,
        "pvcs": [pvc_index[name] for name in sorted(pvc_index)],
    })


def fixture_summary(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        summary: Dict[str, Any] = {"type": "object", "top_level_keys": sorted(data)}
        for key in ("schema",):
            if isinstance(data.get(key), str):
                summary[key] = data[key]
        for key in ("cases", "events", "nodes"):
            value = data.get(key)
            if isinstance(value, list):
                summary["%s_count" % key] = len(value)
            elif isinstance(value, dict):
                summary["%s_count" % key] = len(value)
        return summary
    if isinstance(data, list):
        return {"type": "array", "length": len(data)}
    return {"type": type(data).__name__}


def corpus_fixture_metadata() -> Dict[str, Any]:
    files = []
    for base in SAFE_FIXTURE_ROOTS:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.json")):
            rel = relpath(path)
            if rel in FIXTURE_EXCLUDES:
                continue
            raw = path.read_bytes()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                parsed = None
            row = file_record(rel)
            if parsed is not None:
                row["summary"] = fixture_summary(parsed)
            files.append(row)
    return with_hash({
        "metadata_only": True,
        "privacy_note": (
            "Representative fixture inventory only: repo paths, top-level schema ids, counts, "
            "sizes, and sha256 digests. No live corpus pulls and no payload bodies are copied."
        ),
        "files": files,
    })


def build_snapshot() -> Dict[str, Any]:
    snapshot = {
        "baseline_version": VERSION,
        "title": "Phase 0 contract freeze",
        "inventory_scope": {
            "mode": "repository-visible contracts only",
            "runtime_behavior_changed": False,
            "excludes": [
                "live secrets",
                "cluster state",
                "Redis contents",
                "private or bulk corpus payloads",
                "fixture sample values",
            ],
        },
        "compatibility": {
            "guarantee": (
                "This freeze adds read-only tooling and versioned baseline artifacts only; it does "
                "not change wire formats, firmware behavior, MQTT topics, Redis keys, or manifests."
            ),
            "forward_path": [
                "Regenerate the JSON and markdown baselines before any contract change and diff them in review.",
                "Allocate new wire profile ids or schema generations instead of editing deployed meanings in place.",
                "Document transport, Redis, or manifest layout changes beside the code that changes them.",
            ],
            "rollback_path": [
                "Revert the contract-changing commit or restore the earlier versioned baseline files.",
                "Redeploy the prior firmware tag or manifest set if an operational change shipped with the contract change.",
                "Run `python3 tools/freeze_contracts.py --check` to confirm the checkout matches the frozen baseline again.",
            ],
        },
        "wire_profiles": wire_profiles(),
        "firmware_build_metadata": firmware_build_metadata(),
        "schemas": schemas(),
        "mqtt_topics": mqtt_topics(),
        "redis_keys": redis_keys(),
        "kubernetes_and_pvc_layout": k8s_layout(),
        "corpus_fixture_metadata": corpus_fixture_metadata(),
    }
    snapshot["baseline_hash"] = object_hash(snapshot)
    return snapshot


def md_table(headers: List[str], rows: List[List[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(out)


def render_markdown(snapshot: Mapping[str, Any]) -> str:
    lines: List[str] = [
        "# Phase 0 contract freeze v1",
        "",
        "Repository-visible baseline only. This inventory deliberately excludes live secrets, "
        "cluster reads, Redis contents, private corpus payloads, and raw fixture bodies.",
        "",
        "## Reproduce",
        "",
        "```bash",
        "python3 tools/freeze_contracts.py --format json > docs/data/phase0-freeze-contracts.v1.json",
        "python3 tools/freeze_contracts.py --format markdown > docs/phase0-freeze-contracts.v1.md",
        "python3 tools/freeze_contracts.py --check",
        "```",
        "",
        "## Section hashes",
        "",
        md_table(
            ["section", "sha256"],
            [
                ["baseline", snapshot["baseline_hash"]],
                ["wire_profiles", snapshot["wire_profiles"]["section_hash"]],
                ["firmware_build_metadata", snapshot["firmware_build_metadata"]["section_hash"]],
                ["schemas", snapshot["schemas"]["section_hash"]],
                ["mqtt_topics", snapshot["mqtt_topics"]["section_hash"]],
                ["redis_keys", snapshot["redis_keys"]["section_hash"]],
                ["kubernetes_and_pvc_layout", snapshot["kubernetes_and_pvc_layout"]["section_hash"]],
                ["corpus_fixture_metadata", snapshot["corpus_fixture_metadata"]["section_hash"]],
            ],
        ),
        "",
        "## Compatibility",
        "",
        snapshot["compatibility"]["guarantee"],
        "",
        "### Forward path",
        "",
    ]
    lines.extend("- " + item for item in snapshot["compatibility"]["forward_path"])
    lines.extend([
        "",
        "### Rollback path",
        "",
    ])
    lines.extend("- " + item for item in snapshot["compatibility"]["rollback_path"])

    wp = snapshot["wire_profiles"]
    lines.extend([
        "",
        "## Wire profiles",
        "",
        md_table(
            ["id", "legacy", "bands", "frames", "fs_hz", "layout", "wire bytes", "profile hash"],
            [
                [
                    row["profile_id"],
                    "yes" if row["legacy_read_only"] else "no",
                    row["bands"],
                    row["frames"],
                    row["fs_hz"],
                    row["layout"],
                    row["wire_size_bytes"],
                    row["profile_hash"],
                ]
                for row in wp["profiles"]
            ],
        ),
        "",
        "Source hashes:",
        "",
        md_table(
            ["path", "size_bytes", "sha256"],
            [[f["path"], f["size_bytes"], f["sha256"]] for f in wp["source_files"]],
        ),
    ])

    fb = snapshot["firmware_build_metadata"]
    lines.extend([
        "",
        "## Firmware and build metadata",
        "",
        md_table(
            ["arduino_cli", "esp32_core", "default_board_class", "fqbn"],
            [[fb["toolchain"]["arduino_cli_version"], fb["toolchain"]["esp32_core"],
              fb["default_board_class"], fb["fqbn"]]],
        ),
        "",
        md_table(
            ["board_class", "release_stem", "cpp_flags"],
            [[b["board_class"], b["release_stem"], " ".join(b["cpp_flags"]) or "(none)"]
             for b in fb["board_profiles"]],
        ),
        "",
        md_table(
            ["sketch", "board_class", "guarded", "artifact", "build_flags"],
            [[b["sketch"], b["board_class"] or "(none)", "yes" if b["guarded"] else "no",
              b["artifact"], " ".join(b["build_flags"]) or "(none)"]
             for b in fb["workflow_builds"]],
        ),
    ])

    sc = snapshot["schemas"]
    lines.extend([
        "",
        "## Schemas",
        "",
        md_table(
            ["contract", "current"],
            [
                ["pool schema version", sc["pool_schema_version"]],
                ["receiver schema version", sc["receiver_schema_version"]],
                ["telemetry schema version", sc["telemetry_schema_version"]],
                ["dets.csv latest", sc["dets_csv"]["latest_generation"]],
                ["scene.csv latest", sc["scene_csv"]["latest_generation"]],
            ],
        ),
        "",
        "### Telemetry routes",
        "",
        md_table(
            ["http_path", "telemetry_path", "fields", "event_types", "route_hash"],
            [
                [
                    route["http_path"],
                    route["telemetry_path"],
                    len(route["required_fields"]),
                    ", ".join(route.get("event_types", [])) or "(n/a)",
                    route["route_hash"],
                ]
                for route in sc["telemetry_routes"]
            ],
        ),
        "",
        "### Literal schema identifiers found in repo",
        "",
        md_table(
            ["schema", "source_paths"],
            [[item["schema"], "<br>".join(item["source_paths"])] for item in sc["schema_identifiers"]],
        ),
    ])

    mt = snapshot["mqtt_topics"]
    lines.extend([
        "",
        "## MQTT topics",
        "",
        md_table(
            ["topic_pattern", "role", "source_paths"],
            [[t["topic_pattern"], t["role"], "<br>".join(t["source_paths"])] for t in mt["topics"]],
        ),
    ])

    rk = snapshot["redis_keys"]
    lines.extend([
        "",
        "## Redis keys",
        "",
        md_table(
            ["pattern", "kind", "writer", "ttl/maxlen"],
            [[
                item["pattern"],
                item["kind"],
                item["writer"],
                item.get("ttl_seconds", item.get("maxlen", "(n/a)")),
            ] for item in rk["keys"]],
        ),
    ])

    kz = snapshot["kubernetes_and_pvc_layout"]
    lines.extend([
        "",
        "## Kubernetes and PVC layout",
        "",
        "Manifest hashes:",
        "",
        md_table(
            ["path", "size_bytes", "sha256"],
            [[m["path"], m["size_bytes"], m["sha256"]] for m in kz["manifest_files"]],
        ),
        "",
        "### PVCs",
        "",
        md_table(
            ["claim", "storage", "access_modes", "consumers", "paths"],
            [[
                pvc["claim_name"],
                pvc["storage_request"],
                ", ".join(pvc["access_modes"]) or "(unknown)",
                "<br>".join(
                    "%s %s %s:%s" % (
                        c["kind"], c["object_name"], c["container"], c["mount_path"],
                    ) for c in pvc["consumers"]
                ) or "(none)",
                "<br>".join(pvc["path_references"]) or "(none)",
            ] for pvc in kz["pvcs"]],
        ),
        "",
        "### Objects",
        "",
        md_table(
            ["manifest", "kind", "name", "schedule", "hostNetwork", "containers"],
            [[
                obj["manifest"],
                obj["kind"],
                obj["name"],
                obj.get("schedule", "(n/a)"),
                "yes" if obj.get("host_network") else "no",
                "<br>".join(c["name"] for c in obj.get("containers", [])) or "(n/a)",
            ] for obj in kz["objects"]],
        ),
    ])

    cf = snapshot["corpus_fixture_metadata"]
    lines.extend([
        "",
        "## Representative corpus fixture metadata",
        "",
        cf["privacy_note"],
        "",
        md_table(
            ["path", "schema", "summary", "size_bytes", "sha256"],
            [[
                item["path"],
                item.get("summary", {}).get("schema", "(none)"),
                json.dumps(item.get("summary", {}), sort_keys=True),
                item["size_bytes"],
                item["sha256"],
            ] for item in cf["files"]],
        ),
        "",
    ])
    return "\n".join(lines)


def check_outputs() -> int:
    snapshot = build_snapshot()
    expected_json = stable_json(snapshot) + "\n"
    expected_md = render_markdown(snapshot) + "\n"
    problems = []
    if JSON_OUTPUT.read_text() != expected_json:
        problems.append("%s is stale; regenerate with --format json" % relpath(JSON_OUTPUT))
    if MARKDOWN_OUTPUT.read_text() != expected_md:
        problems.append("%s is stale; regenerate with --format markdown" % relpath(MARKDOWN_OUTPUT))
    if problems:
        sys.stderr.write("\n".join(problems) + "\n")
        return 1
    return 0


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--format", choices=("json", "markdown"), default="json")
    ap.add_argument("--check", action="store_true", help="verify checked-in baseline artifacts")
    return ap.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check:
        return check_outputs()
    snapshot = build_snapshot()
    if args.format == "json":
        sys.stdout.write(stable_json(snapshot) + "\n")
    else:
        sys.stdout.write(render_markdown(snapshot) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
