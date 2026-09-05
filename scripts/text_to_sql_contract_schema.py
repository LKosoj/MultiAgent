#!/usr/bin/env python3
"""Emit the Text-to-SQL terminal contract as self-contained JSON for TS sync.

``build_schema()`` derives its output solely from ``workflow.text_to_sql_contract``
(terminal statuses, reason codes, required fields) so the frontend never has to
import Python to stay in sync with the authoritative contract.

Usage:
    python3 scripts/text_to_sql_contract_schema.py           # (re)writes the fixture
    python3 scripts/text_to_sql_contract_schema.py --check   # verifies it is current

Procedure when the contract changes (see docs/text_to_sql_contracts.md for the
full write-up, including the adjacent adaptive/models.py contract_name family):
1. edit workflow/text_to_sql_contract.py;
2. run this script without --check to regenerate the fixture;
3. sync frontend/client/src/app/lib/textToSqlContracts.ts by hand;
4. update docs/text_to_sql_contracts.md;
5. rerun the sync gates: tests/test_text_to_sql_contract_schema_sync.py,
   frontend/.../textToSqlContractsSync.test.ts,
   tests/test_text_to_sql_contracts_documented.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow.text_to_sql_contract import (  # noqa: E402
    TEXT_TO_SQL_TERMINAL_REQUIRED_FIELDS,
    TextToSqlTerminalReasonCode,
    TextToSqlTerminalStatus,
    _LEGACY_OPTIONAL_TERMINAL_FIELDS,
)

DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "tests" / "fixtures" / "text_to_sql_contract_schema.json"


def build_schema() -> dict:
    """Return the JSON-serializable Text-to-SQL terminal contract description."""
    return {
        "terminal_statuses": sorted(status.value for status in TextToSqlTerminalStatus),
        "reason_codes": sorted(code.value for code in TextToSqlTerminalReasonCode),
        "required_fields": sorted(TEXT_TO_SQL_TERMINAL_REQUIRED_FIELDS),
        "legacy_optional_fields": sorted(_LEGACY_OPTIONAL_TERMINAL_FIELDS),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the fixture matches build_schema() without writing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    schema = build_schema()

    if arguments.check:
        try:
            on_disk = json.loads(arguments.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"schema fixture missing: {arguments.path}", file=sys.stderr)
            return 1
        if on_disk != schema:
            print(f"schema fixture is stale: {arguments.path}", file=sys.stderr)
            return 1
        print(f"{arguments.path} is current")
        return 0

    arguments.path.parent.mkdir(parents=True, exist_ok=True)
    arguments.path.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {arguments.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
