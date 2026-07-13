"""Text-to-SQL UI backed exclusively by the authenticated AG-UI HTTP API."""

from __future__ import annotations

import os
from typing import Any, Mapping
import uuid

import streamlit as st
from streamlit_app._theme import inject_theme
from streamlit_app.text_to_sql_client import (
    TERMINAL_RUN_STATUSES,
    ConnectionSummary,
    RunStatus,
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
    use_schema_suggestions: bool,
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
            use_schema_suggestions=use_schema_suggestions,
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
    if explanation:
        st.markdown("#### Объяснение")
        st.write(explanation)
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
        use_schema_suggestions = st.checkbox("Использовать подсказки схемы", value=True)
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
                use_schema_suggestions=use_schema_suggestions,
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
    try:
        connections = _authorized_connections(client)
    except TextToSqlApiError as exc:
        st.error(str(exc))
        st.stop()
        return

    generation_tab, connections_tab, schema_tab, history_tab = st.tabs(
        ["🔍 Генерация SQL", "🔌 Подключения", "📊 Схема БД", "📚 История"]
    )
    with generation_tab:
        show_sql_generation(client, connections)
    with connections_tab:
        show_database_connections(connections)
    with schema_tab:
        show_database_schema(client)
    with history_tab:
        show_sql_history(client)


if __name__ == "__main__":
    main()
