"""Text-to-SQL UI backed exclusively by the authenticated AG-UI HTTP API."""

from __future__ import annotations

import os
from typing import Any, Mapping
import uuid

import streamlit as st
from streamlit_app._theme import inject_theme
from workflow.text_to_sql_provenance import format_text_to_sql_provenance_footer
from streamlit_app.text_to_sql_client import (
    TERMINAL_RUN_STATUSES,
    ConnectionSummary,
    GlossaryEntrySummary,
    MetadataView,
    RunStatus,
    SemanticFactSummary,
    TableMetadata,
    TextToSqlApiClient,
    TextToSqlApiError,
    TextToSqlResult,
    TextToSqlRunRequest,
)


def _validate_text_to_sql_options(
    max_rows: Any,
    safety_level: str,
) -> tuple[int, str]:
    if isinstance(max_rows, bool):
        raise ValueError("max_rows должен быть целым числом")
    if isinstance(max_rows, int):
        normalized_max_rows = max_rows
    elif isinstance(max_rows, float) and max_rows.is_integer():
        normalized_max_rows = int(max_rows)
    elif isinstance(max_rows, str) and max_rows.strip().isdigit():
        normalized_max_rows = int(max_rows.strip())
    else:
        raise ValueError("max_rows должен быть целым числом")
    if normalized_max_rows < 1 or normalized_max_rows > 10000:
        raise ValueError("max_rows должен быть от 1 до 10000")
    normalized_safety = str(safety_level or "strict").strip().lower()
    if normalized_safety != "strict":
        raise ValueError("Поддерживается только safety_level=strict")
    return normalized_max_rows, normalized_safety


def _api_auth_headers() -> Mapping[str, str]:
    session_headers = st.session_state.get("api_auth_headers")
    if isinstance(session_headers, Mapping) and session_headers:
        return {str(key): str(value) for key, value in session_headers.items()}
    token = (
        st.session_state.get("api_token")
        or os.getenv("MULTIAGENT_API_TOKEN")
        or os.getenv("AG_UI_USER_TOKEN")
        or os.getenv("AG_UI_AUTH_TOKEN")
    )
    if isinstance(token, str) and token.strip():
        return {"Authorization": f"Bearer {token.strip()}"}
    return {}


def _api_client() -> TextToSqlApiClient:
    return TextToSqlApiClient(
        base_url=os.getenv("MULTIAGENT_API_URL", "http://127.0.0.1:8000"),
        auth_headers=_api_auth_headers,
    )


def init_session_state() -> None:
    defaults = {
        "selected_connection_ref": "",
        "agent_run_id": "",
        "agent_run_status": None,
        "text_to_sql_result": None,
        "text_to_sql_query": "",
        "text_to_sql_max_rows": None,
        "current_schema": None,
        "current_metadata": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _authorized_connections(client: TextToSqlApiClient) -> list[ConnectionSummary]:
    connections = client.list_connections()
    authorized_refs = {connection.connection_ref for connection in connections}
    selected = st.session_state.selected_connection_ref
    if selected not in authorized_refs:
        selected = ""
    try:
        query_param = st.query_params.get("conn")
        if isinstance(query_param, list):
            query_param = query_param[0] if query_param else ""
        if isinstance(query_param, str) and query_param in authorized_refs:
            selected = query_param
    except Exception:
        pass
    st.session_state.selected_connection_ref = selected
    return connections


def generate_sql_query(
    client: TextToSqlApiClient,
    *,
    natural_query: str,
    connection_ref: str,
    max_rows: Any,
    safety_level: str,
    include_explanation: bool,
    validate_schema: bool,
    dry_run_only: bool,
) -> None:
    normalized_rows, normalized_safety = _validate_text_to_sql_options(
        max_rows,
        safety_level,
    )
    handle = client.start(
        TextToSqlRunRequest(
            query=natural_query,
            connection_ref=connection_ref,
            idempotency_key=str(uuid.uuid4()),
            max_rows=normalized_rows,
            safety_level=normalized_safety,
            include_explanation=include_explanation,
            validate_schema=validate_schema,
            dry_run_only=dry_run_only,
        )
    )
    st.session_state.agent_run_id = handle.run_id
    st.session_state.agent_run_status = "pending"
    st.session_state.text_to_sql_result = None
    st.session_state.text_to_sql_query = natural_query
    st.session_state.text_to_sql_max_rows = normalized_rows


def _refresh_run(
    client: TextToSqlApiClient,
) -> tuple[RunStatus, TextToSqlResult | None]:
    run_id = st.session_state.agent_run_id
    status = client.get_run(run_id)
    st.session_state.agent_run_status = status.status
    result = None
    if status.status in TERMINAL_RUN_STATUSES:
        result = client.get_result(run_id)
        st.session_state.text_to_sql_result = result
    return status, result


_MARKDOWN_SPECIAL_CHARS = "\\`*_[]#<>"


def _escape_markdown(text: str) -> str:
    """Escape Markdown control characters so ``st.markdown`` renders them literally.

    Clarification options come from verified model state, not the user, but
    ``st.markdown`` still interprets ``_``/``*``/`` ` ``/etc. as formatting
    (e.g. ``orders.status_code`` would render with a spurious italic). No
    ``unsafe_allow_html`` is used, so this only prevents mis-rendering, not
    injection.
    """
    return "".join(
        f"\\{char}" if char in _MARKDOWN_SPECIAL_CHARS else char for char in text
    )


def _render_result(result: TextToSqlResult) -> None:
    st.markdown("### Результат")
    st.write(f"Статус: `{result.status}`")
    if result.reason_code:
        st.write(f"Причина: `{result.reason_code}`")
        if not result.reason_code_recognized:
            st.caption(f"Неизвестный код причины: {result.reason_code}")
    if result.sql:
        st.code(result.sql, language="sql")
    if result.rows:
        st.dataframe(result.rows, use_container_width=True)
    explanation = result.final_output
    if isinstance(explanation, Mapping):
        outputs = explanation.get("outputs")
        clarification = (
            outputs.get("clarification_needed")
            if isinstance(outputs, Mapping)
            else None
        )
        if isinstance(clarification, Mapping) and clarification.get("question"):
            st.warning(str(clarification["question"]))
            options = clarification.get("options")
            if isinstance(options, list) and options:
                st.markdown(
                    "\n".join(
                        f"- {_escape_markdown(str(option))}" for option in options
                    )
                )
    if explanation:
        st.markdown("#### Объяснение")
        st.write(explanation)
    footer = format_text_to_sql_provenance_footer(result.raw.get("provenance", {}))
    if footer:
        st.caption(_escape_markdown(footer))
    error_message = result.raw.get("error")
    if error_message:
        st.error(str(error_message))


def show_sql_generation(
    client: TextToSqlApiClient,
    connections: list[ConnectionSummary],
) -> None:
    st.markdown("## Генерация SQL")
    if not connections:
        st.warning("Нет доступных подключений. Обратитесь к администратору.")
        return

    labels = {
        connection.connection_ref: (
            connection.display_name or connection.connection_ref
        )
        for connection in connections
    }
    refs = list(labels)
    current_ref = st.session_state.selected_connection_ref
    selected_index = refs.index(current_ref) if current_ref in refs else 0
    selected_ref = st.selectbox(
        "Подключение",
        refs,
        index=selected_index,
        format_func=lambda ref: labels[ref],
    )
    st.session_state.selected_connection_ref = selected_ref

    with st.form("text_to_sql_request"):
        natural_query = st.text_area(
            "Запрос на естественном языке",
            placeholder="Например: покажи продажи по месяцам",
        )
        max_rows = st.text_input("Максимум строк", value="100")
        include_explanation = st.checkbox("Добавить объяснение", value=True)
        validate_schema = st.checkbox("Проверять схему", value=True)
        dry_run_only = st.checkbox("Только подготовить SQL", value=False)
        submitted = st.form_submit_button("Запустить")

    if submitted:
        try:
            generate_sql_query(
                client,
                natural_query=natural_query,
                connection_ref=selected_ref,
                max_rows=max_rows,
                safety_level="strict",
                include_explanation=include_explanation,
                validate_schema=validate_schema,
                dry_run_only=dry_run_only,
            )
            st.success(f"Запуск создан: {st.session_state.agent_run_id}")
        except (ValueError, TextToSqlApiError) as exc:
            st.error(str(exc))

    run_id = st.session_state.agent_run_id
    if not run_id:
        return
    st.markdown(f"Текущий запуск: `{run_id}`")
    refresh_col, cancel_col = st.columns(2)
    with refresh_col:
        refresh = st.button("Обновить статус", key="refresh_text_to_sql")
    with cancel_col:
        cancel = st.button("Отменить", key="cancel_text_to_sql")
    try:
        if cancel:
            status = client.cancel(run_id)
            st.session_state.agent_run_status = status.status
        if refresh or st.session_state.text_to_sql_result is None:
            status, result = _refresh_run(client)
            st.write(f"Статус запуска: `{status.status}`")
            if result is not None:
                _render_result(result)
        elif st.session_state.text_to_sql_result is not None:
            _render_result(st.session_state.text_to_sql_result)
    except TextToSqlApiError as exc:
        st.error(str(exc))


def show_database_connections(connections: list[ConnectionSummary]) -> None:
    st.markdown("## Доступные подключения")
    if not connections:
        st.info("Подключений нет.")
        return
    for connection in connections:
        label = connection.display_name or connection.connection_ref
        selected = connection.connection_ref == st.session_state.selected_connection_ref
        cols = st.columns([3, 1])
        with cols[0]:
            st.write(f"**{label}**")
            st.caption(connection.connection_ref)
        with cols[1]:
            if selected:
                st.success("Выбрано")
            elif st.button("Выбрать", key=f"select-{connection.connection_ref}"):
                st.session_state.selected_connection_ref = connection.connection_ref
                st.rerun()


def show_database_schema(client: TextToSqlApiClient) -> None:
    st.markdown("## Схема БД")
    connection_ref = st.session_state.selected_connection_ref
    if not connection_ref:
        st.info("Сначала выберите подключение.")
        return
    if st.button("Загрузить схему"):
        try:
            st.session_state.current_schema = client.load_schema(connection_ref)
        except TextToSqlApiError as exc:
            st.error(str(exc))
    schema = st.session_state.current_schema
    if schema is None:
        return
    for table in schema.tables:
        name = table.get("name") or table.get("table_name") or "table"
        with st.expander(str(name)):
            st.json(table)


def _build_description_rows(tables: list[TableMetadata]) -> list[dict[str, Any]]:
    """Одна строка на описание таблицы (``column=""``) плюс строка на каждую колонку."""
    rows: list[dict[str, Any]] = []
    for table in tables:
        rows.append(
            {
                "table": table.table_fqn,
                "column": "",
                "description": table.description,
                "examples": "",
            }
        )
        for column in table.columns:
            rows.append(
                {
                    "table": table.table_fqn,
                    "column": column.name,
                    "description": column.description,
                    "examples": ", ".join(str(example) for example in column.examples),
                }
            )
    return rows


def _parse_examples_text(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _diff_description_edits(
    original_rows: list[dict[str, Any]],
    edited_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Строит частичный payload §1.3: только реально изменённые таблицы/колонки."""
    table_edits: dict[str, dict[str, Any]] = {}
    for original, edited in zip(original_rows, edited_rows):
        table_fqn = str(original["table"])
        column = str(original["column"])
        original_description = str(original["description"])
        edited_description = str(edited.get("description") or "")
        original_examples = str(original["examples"])
        edited_examples = str(edited.get("examples") or "")
        description_changed = edited_description != original_description
        examples_changed = bool(column) and edited_examples != original_examples
        if not description_changed and not examples_changed:
            continue
        table_edit = table_edits.setdefault(
            table_fqn, {"table_fqn": table_fqn, "columns": []}
        )
        if not column:
            if description_changed:
                table_edit["description"] = edited_description
        else:
            column_edit: dict[str, Any] = {"column": column}
            if description_changed:
                column_edit["description"] = edited_description
            if examples_changed:
                column_edit["examples"] = _parse_examples_text(edited_examples)
            table_edit["columns"].append(column_edit)
    return list(table_edits.values())


def _build_glossary_rows(entries: list[GlossaryEntrySummary]) -> list[dict[str, Any]]:
    return [
        {
            "term": entry.term,
            "synonyms": ", ".join(entry.synonyms),
            "table": entry.table,
            "column": entry.column or "",
            "kind": entry.kind or "",
            "note": entry.note or "",
        }
        for entry in entries
    ]


def _parse_glossary_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Полная замена глоссария (§1.4): пустые новые строки редактора отбрасываются."""
    entries: list[dict[str, Any]] = []
    for row in rows:
        term = str(row.get("term") or "").strip()
        table = str(row.get("table") or "").strip()
        if not term and not table:
            continue
        if not term:
            raise ValueError("У термина глоссария должно быть заполнено поле «term»")
        if not table:
            raise ValueError(f"У термина «{term}» должно быть заполнено поле «table»")
        synonyms = [
            item.strip()
            for item in str(row.get("synonyms") or "").split(",")
            if item.strip()
        ]
        entries.append(
            {
                "term": term,
                "synonyms": synonyms,
                "table": table,
                "column": str(row.get("column") or "").strip() or None,
                "kind": str(row.get("kind") or "").strip() or None,
                "note": str(row.get("note") or "").strip() or None,
            }
        )
    return entries


def _handle_metadata_save_error(
    client: TextToSqlApiClient,
    connection_ref: str,
    exc: TextToSqlApiError,
) -> None:
    # Backend-сообщение о конфликте версии (§1.3/§1.4) содержит "version conflict" —
    # отличаем его от прочих ошибок валидации, чтобы предложить перезагрузку.
    if "version conflict" in str(exc):
        st.error(f"{exc} Перезагрузите метаданные и повторите правку.")
        if st.button("Перезагрузить метаданные", key="reload_metadata_after_conflict"):
            st.session_state.current_metadata = client.load_metadata(connection_ref)
            st.rerun()
    else:
        st.error(str(exc))


def show_metadata_editor(client: TextToSqlApiClient, *, is_admin: bool) -> None:
    st.markdown("## Метаданные схемы")
    connection_ref = st.session_state.selected_connection_ref
    if not connection_ref:
        st.info("Сначала выберите подключение.")
        return
    if st.button("Загрузить метаданные", key="load_metadata"):
        try:
            st.session_state.current_metadata = client.load_metadata(connection_ref)
        except TextToSqlApiError as exc:
            st.error(str(exc))

    metadata: MetadataView | None = st.session_state.current_metadata
    if metadata is None:
        return
    if not is_admin:
        st.caption(
            "Сохранение метаданных доступно только пользователям с ролью admin."
        )

    st.markdown("### Описания таблиц и колонок")
    if metadata.editable_file_enabled is False:
        st.warning(
            "Файл описаний для этого подключения отключён (enable: false): "
            "сохранённые описания не попадут в пайплайн, пока файл не будет включён."
        )
    original_description_rows = _build_description_rows(metadata.tables)
    edited_description_rows = st.data_editor(
        original_description_rows,
        num_rows="fixed",
        disabled=["table", "column"],
        use_container_width=True,
        key="metadata_description_editor",
    )
    if st.button(
        "Сохранить описания", disabled=not is_admin, key="save_descriptions"
    ):
        table_edits = _diff_description_edits(
            original_description_rows, edited_description_rows
        )
        if not table_edits:
            st.info("Нет изменений для сохранения.")
        else:
            try:
                new_digest = client.save_metadata_descriptions(
                    connection_ref=connection_ref,
                    expected_schema_digest=metadata.schema_digest,
                    tables=table_edits,
                )
                st.session_state.current_metadata = client.load_metadata(
                    connection_ref
                )
                st.success(f"Описания сохранены (digest: {new_digest}).")
            except TextToSqlApiError as exc:
                _handle_metadata_save_error(client, connection_ref, exc)

    st.markdown("### Глоссарий")
    original_glossary_rows = _build_glossary_rows(metadata.glossary_entries)
    edited_glossary_rows = st.data_editor(
        original_glossary_rows,
        num_rows="dynamic",
        use_container_width=True,
        key="metadata_glossary_editor",
        column_config={
            "kind": st.column_config.SelectboxColumn(
                "kind",
                options=["", "dimension", "measure", "filter_value", "entity"],
            ),
        },
    )
    if st.button("Сохранить глоссарий", disabled=not is_admin, key="save_glossary"):
        try:
            entries = _parse_glossary_rows(edited_glossary_rows)
            new_digest = client.save_metadata_glossary(
                connection_ref=connection_ref,
                expected_glossary_digest=metadata.glossary_digest,
                entries=entries,
            )
            st.session_state.current_metadata = client.load_metadata(connection_ref)
            st.success(f"Глоссарий сохранён (digest: {new_digest}).")
        except ValueError as exc:
            st.error(str(exc))
        except TextToSqlApiError as exc:
            _handle_metadata_save_error(client, connection_ref, exc)

    st.markdown("### Семантические факты")
    facts: list[SemanticFactSummary] = metadata.facts
    if not facts:
        st.info("Фактов нет.")
    for fact in facts:
        subject = fact.table_fqn + (f".{fact.column}" if fact.column else "")
        with st.expander(f"{subject} · {fact.fact_kind} · {fact.status}"):
            st.write(f"Значение: {fact.value!r}")
            new_status = st.selectbox(
                "Статус",
                ["approved", "rejected"],
                index=0 if fact.status == "approved" else 1,
                key=f"fact_status_{fact.fact_key}",
            )
            if st.button(
                "Применить",
                disabled=not is_admin,
                key=f"fact_apply_{fact.fact_key}",
            ):
                try:
                    client.set_metadata_fact_status(
                        connection_ref=connection_ref,
                        fact_key=fact.fact_key,
                        status=new_status,
                    )
                    st.session_state.current_metadata = client.load_metadata(
                        connection_ref
                    )
                    st.success("Статус факта обновлён.")
                except TextToSqlApiError as exc:
                    st.error(str(exc))


def show_sql_history(client: TextToSqlApiClient) -> None:
    st.markdown("## История SQL запросов")
    try:
        entries = client.list_history(limit=100)
    except TextToSqlApiError as exc:
        st.error(str(exc))
        return
    if not entries:
        st.info("История пуста.")
    for entry in entries:
        terminal = entry.get("terminal_snapshot")
        terminal = terminal if isinstance(terminal, Mapping) else {}
        title = terminal.get("sql") or entry.get("run_id") or "Запрос"
        with st.expander(f"{entry.get('created_at_ms', '')} — {str(title)[:80]}"):
            st.write(f"Статус: `{entry.get('status', 'unknown')}`")
            if terminal.get("sql"):
                st.code(str(terminal["sql"]), language="sql")
            st.caption(f"Диалект: {entry.get('dialect', '—')}")
            st.caption(f"Профиль: {entry.get('profile_name', '—')}")
            st.caption(f"Run ID: {entry.get('run_id', '—')}")
    if entries and st.button("Очистить историю"):
        client.clear_history()
        st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="Text-to-SQL - MultiAgent System",
        page_icon="🔍",
        layout="wide",
    )
    inject_theme()
    client = _api_client()
    try:
        principal = client.get_me()
    except TextToSqlApiError as exc:
        st.error(f"Требуется авторизованная сессия backend API: {exc}")
        st.stop()
        return

    init_session_state()
    st.title("🔍 Text-to-SQL генерация и выполнение")
    subject = principal.get("subject") if isinstance(principal, Mapping) else None
    if subject:
        st.caption(f"Пользователь: {subject}")
    roles = principal.get("roles") if isinstance(principal, Mapping) else None
    is_admin = isinstance(roles, list) and "admin" in roles
    try:
        connections = _authorized_connections(client)
    except TextToSqlApiError as exc:
        st.error(str(exc))
        st.stop()
        return

    (
        generation_tab,
        connections_tab,
        schema_tab,
        history_tab,
        metadata_tab,
    ) = st.tabs(
        [
            "🔍 Генерация SQL",
            "🔌 Подключения",
            "📊 Схема БД",
            "📚 История",
            "🗂️ Метаданные",
        ]
    )
    with generation_tab:
        show_sql_generation(client, connections)
    with connections_tab:
        show_database_connections(connections)
    with schema_tab:
        show_database_schema(client)
    with history_tab:
        show_sql_history(client)
    with metadata_tab:
        show_metadata_editor(client, is_admin=is_admin)


if __name__ == "__main__":
    main()
