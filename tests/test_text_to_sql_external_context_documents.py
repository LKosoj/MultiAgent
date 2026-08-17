"""Public context documents stay outside the natural-language question."""

import pytest

from backend.fastapi_app.agui._t2s_requests import parse_text_to_sql_start


def test_context_documents_are_a_closed_text_list_separate_from_query() -> None:
    request = parse_text_to_sql_start(
        {
            "query": "What is the ratio?",
            "context_documents": ["Count rows where gross_value is 1 divided by rows where it is 2."],
            "connection_ref": "connection-1",
        }
    )

    assert request.query == "What is the ratio?"
    assert request.context_documents == (
        "Count rows where gross_value is 1 divided by rows where it is 2.",
    )


@pytest.mark.parametrize("documents", ([""], [{"content": "rule"}]))
def test_context_documents_reject_non_text_or_empty_entries(documents: object) -> None:
    with pytest.raises(ValueError):
        parse_text_to_sql_start(
            {
                "query": "What is the ratio?",
                "context_documents": documents,
                "connection_ref": "connection-1",
            }
        )
