"""Shared Chroma document text selection for memory tactical records."""

from __future__ import annotations

from typing import Any, Callable


def extract_tactical_chroma_text(
    data: Any,
    *,
    fallback_extractor: Callable[[Any], str],
) -> str:
    """Return the exact text that should be embedded for a tactical row."""

    if not isinstance(data, dict):
        return fallback_extractor(data)

    cache_kind = data.get("cache_kind")

    if cache_kind == "agent_step":
        agent_response = data.get("agent_response")
        if isinstance(agent_response, str) and agent_response.strip():
            return agent_response.strip()
        slim = {
            key: value
            for key, value in data.items()
            if key not in ("timestamp", "policy_scope", "agent_context")
        }
        return fallback_extractor(slim)

    if cache_kind == "agent_summary":
        summary_text = data.get("summary_text")
        if isinstance(summary_text, str) and summary_text.strip():
            return summary_text.strip()
        return fallback_extractor(data)

    if cache_kind == "schema_table":
        table_fqn = data.get("table_fqn") or ""
        description = data.get("description") or ""
        columns: list[str] = []
        try:
            table_info = data.get("table_info") or {}
            if isinstance(table_info, dict):
                for column in table_info.get("columns") or []:
                    if not isinstance(column, dict):
                        continue
                    name = column.get("name")
                    column_description = column.get("description")
                    if not name:
                        continue
                    if isinstance(column_description, str) and column_description.strip():
                        columns.append(f"{name}: {column_description.strip()}")
                    else:
                        columns.append(str(name))
        except Exception:
            columns = []
        columns_text = "; ".join(columns[:40])
        text = f"Таблица {table_fqn}. {description}".strip()
        if columns_text:
            text = f"{text}\nКолонки: {columns_text}"
        return text[:8000]

    return fallback_extractor(data)
