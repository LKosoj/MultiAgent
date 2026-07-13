#!/usr/bin/env python3
"""Canonicalize CycloneDX output for reproducibility comparison."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [_canonicalize(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    return value


def normalize_sbom(
    source: Path,
    destination: Path,
    expected_component_version: str | None = None,
) -> None:
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("SBOM root must be an object")
    if payload.get("bomFormat") != "CycloneDX":
        raise ValueError("SBOM must use CycloneDX format")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("CycloneDX SBOM metadata must be an object")
    component = metadata.get("component")
    if not isinstance(component, dict) or not isinstance(
        component.get("version"), str
    ):
        raise ValueError("CycloneDX metadata.component.version is required")
    if (
        expected_component_version is not None
        and component["version"] != expected_component_version
    ):
        raise ValueError("CycloneDX component version does not match runtime image")
    payload.pop("serialNumber", None)
    metadata.pop("timestamp", None)
    component.pop("name", None)
    destination.write_text(
        json.dumps(
            _canonicalize(payload),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("expected_component_version", nargs="?")
    args = parser.parse_args()
    normalize_sbom(
        args.source,
        args.destination,
        args.expected_component_version,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
