from __future__ import annotations

from custom_tools.text_to_sql import successful_sql_memory
from custom_tools.text_to_sql.core import save_successful_sql


def test_successful_sql_retrieval_is_empty_when_runtime_policy_disables_it(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED", "0")

    def forbidden() -> None:
        raise AssertionError("repository must not be opened")

    monkeypatch.setattr(
        successful_sql_memory,
        "SuccessfulSqlMemoryRepository",
        forbidden,
    )

    result = successful_sql_memory.successful_sql_retrieval(
        "not-needed-while-disabled",
        "show rows",
    )

    assert result == {
        "status": "EMPTY",
        "examples": [],
        "context_json": "[]",
        "failed_ids": [],
        "error_code": None,
    }


def test_successful_sql_persistence_is_not_attempted_when_runtime_policy_disables_it(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED", "0")

    result = save_successful_sql(
        sql_query="SELECT 1",
        user_query="show one",
    )

    assert result == {
        "status": "error",
        "error": "successful SQL persistence disabled by runtime policy",
    }
