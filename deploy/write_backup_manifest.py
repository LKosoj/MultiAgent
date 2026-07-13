#!/usr/bin/env python3
"""Create the candidate-bound manifest for protected SQLite backups."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_DATABASES = (
    "agui_events.db",
    "smolagents_memory.db",
    "workflow_result_outbox.db",
)


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _database_record(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    finally:
        connection.close()
    integrity = [str(row[0]) for row in integrity_rows]
    if integrity != ["ok"]:
        raise ValueError(f"backup database {path.name} failed integrity_check")
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "user_version": user_version,
        "integrity_check": "ok",
    }


def validate_backup_manifest(
    manifest_path: Path,
    *,
    backup_reference: Path,
    candidate_digest: str,
    schema_ref: str,
    expected_sha256: str,
) -> str:
    if not backup_reference.is_absolute() or not backup_reference.is_dir():
        raise ValueError("backup reference must be an existing absolute directory")
    if _SHA256.fullmatch(candidate_digest) is None:
        raise ValueError("candidate digest must be a lowercase SHA-256")
    if _SCHEMA_REF.fullmatch(schema_ref) is None:
        raise ValueError("schema ref must be a sha256 reference")
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("expected manifest digest must be a lowercase SHA-256")
    if not manifest_path.is_file():
        raise ValueError("backup manifest is missing")

    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != expected_sha256:
        raise ValueError("backup manifest digest does not match approved digest")
    try:
        payload = json.loads(
            manifest_bytes,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("backup manifest must be valid UTF-8 JSON") from error
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "candidate_digest",
        "schema_ref",
        "databases",
    }:
        raise ValueError("backup manifest fields do not match schema v1")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("backup manifest schema_version must be 1")
    if payload["candidate_digest"] != candidate_digest:
        raise ValueError("backup manifest candidate digest does not match")
    if payload["schema_ref"] != schema_ref:
        raise ValueError("backup manifest schema ref does not match")

    entries = payload["databases"]
    if not isinstance(entries, list) or len(entries) != len(_DATABASES):
        raise ValueError("backup manifest databases do not match contract")
    expected_entries: list[dict[str, object]] = []
    for entry, filename in zip(entries, _DATABASES, strict=True):
        if not isinstance(entry, dict) or set(entry) != {
            "filename",
            "sha256",
            "user_version",
            "integrity_check",
        }:
            raise ValueError("backup manifest database fields do not match schema v1")
        if entry["filename"] != filename:
            raise ValueError("backup manifest database filenames do not match contract")
        if not isinstance(entry["sha256"], str) or _SHA256.fullmatch(
            entry["sha256"]
        ) is None:
            raise ValueError("backup manifest database sha256 is invalid")
        if type(entry["user_version"]) is not int:
            raise ValueError("backup manifest database user_version must be an integer")
        if entry["integrity_check"] != "ok":
            raise ValueError("backup manifest database integrity_check must be ok")
        path = backup_reference / filename
        if not path.is_file():
            raise ValueError(f"backup database {filename} is missing")
        expected_entries.append(_database_record(path))
    if entries != expected_entries:
        raise ValueError("backup manifest database metadata does not match backup bytes")
    return manifest_sha256


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-reference", required=True, type=Path)
    parser.add_argument("--candidate-digest", required=True)
    parser.add_argument("--schema-ref", required=True)
    parser.add_argument("--expected-sha256")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--validate", type=Path, metavar="MANIFEST")
    args = parser.parse_args()
    if not args.backup_reference.is_absolute() or not args.backup_reference.is_dir():
        raise ValueError("backup reference must be an existing absolute directory")
    if _SHA256.fullmatch(args.candidate_digest) is None:
        raise ValueError("candidate digest must be a lowercase SHA-256")
    if _SCHEMA_REF.fullmatch(args.schema_ref) is None:
        raise ValueError("schema ref must be a sha256 reference")
    if args.validate is not None:
        if args.expected_sha256 is None:
            raise ValueError("--expected-sha256 is required with --validate")
        print(
            validate_backup_manifest(
                args.validate,
                backup_reference=args.backup_reference,
                candidate_digest=args.candidate_digest,
                schema_ref=args.schema_ref,
                expected_sha256=args.expected_sha256,
            )
        )
        return 0
    if args.expected_sha256 is not None:
        raise ValueError("--expected-sha256 is only valid with --validate")
    databases = []
    for filename in _DATABASES:
        path = args.backup_reference / filename
        if not path.is_file():
            raise ValueError(f"backup database {filename} is missing")
        databases.append(_database_record(path))
    payload = {
        "schema_version": 1,
        "candidate_digest": args.candidate_digest,
        "schema_ref": args.schema_ref,
        "databases": databases,
    }
    assert args.output is not None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
