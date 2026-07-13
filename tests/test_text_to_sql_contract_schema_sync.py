"""Fails if `workflow.text_to_sql_contract` drifted from the committed TS-sync fixture.

The frontend reads ``tests/fixtures/text_to_sql_contract_schema.json`` (built by
``scripts/text_to_sql_contract_schema.py``) to stay aligned with the terminal
statuses/reason codes/required fields owned by the Python contract. This test
catches the case where someone changes the contract enums without regenerating
the fixture via ``python3 scripts/text_to_sql_contract_schema.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.text_to_sql_contract_schema import build_schema

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "text_to_sql_contract_schema.json"


def test_contract_schema_fixture_matches_build_schema() -> None:
    on_disk = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert on_disk == build_schema()
