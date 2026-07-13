"""Каноническая реализация примитивов, продублированных ранее между
``workflow/result_outbox.py`` и ``workflow/result_repository.py``
(``_canonical_payload``), а также между ``workflow/result_outbox.py`` и
``workflow/streamlit_api.py`` (``_validate_private_claim`` /
``_validate_private_workflow_claim`` — одна и та же логика под разными
именами).

Модуль не импортирует ничего из соседних workflow-модулей, поэтому его можно
безопасно импортировать на уровне модуля из любого места без риска
циклических импортов.
"""

from __future__ import annotations

import json
from typing import Any, Optional


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_private_claim(
    supervisor_id: Any,
    attempt_generation: Any,
) -> tuple[Optional[str], Optional[int]]:
    if supervisor_id is None and attempt_generation is None:
        return None, None
    if supervisor_id is None or attempt_generation is None:
        raise ValueError(
            "supervisor_id and attempt_generation must be provided together"
        )
    if (
        not isinstance(supervisor_id, str)
        or not supervisor_id
        or supervisor_id != supervisor_id.strip()
        or len(supervisor_id) > 512
        or not all(character.isprintable() for character in supervisor_id)
    ):
        raise ValueError("supervisor_id must be canonical text")
    if (
        isinstance(attempt_generation, bool)
        or not isinstance(attempt_generation, int)
        or attempt_generation <= 0
    ):
        raise ValueError("attempt_generation must be a positive integer")
    return supervisor_id, attempt_generation
