"""
Contract-pinning тесты для public API ``schema_linking`` package.

История: до Phase 7 / T7.3 существовал монолитный модуль
``schema_linking_core.py``; T7.3 декомпозировал его в подпакет
``schema_linking/``. До EPIC 8.6 ``schema_linking_core.py`` оставался
тонким shim'ом. EPIC 8.6 удалил shim полностью — все внешние импорты
должны идти через пакет ``schema_linking``.
"""


def test_schema_linking_package_exports_class():
    """Канонический импорт после EPIC 8.6."""
    from custom_tools.text_to_sql.schema_linking import SchemaLinkingCore

    assert callable(SchemaLinkingCore)


def test_schema_linking_resolution_functions_exported():
    from custom_tools.text_to_sql.schema_linking.resolution import (
        _column_exists_in_table,
        _get_column_meta,
        _resolve_column_name,
        _resolve_table_name,
        _table_exists_in_schema,
    )

    for fn in (
        _resolve_table_name,
        _resolve_column_name,
        _table_exists_in_schema,
        _column_exists_in_table,
        _get_column_meta,
    ):
        assert callable(fn)


def test_schema_linker_uses_shared_resolution():
    """После dedup ``schema_linker`` импортирует резолверы из общего модуля."""
    import custom_tools.text_to_sql.schema_linker as linker
    from custom_tools.text_to_sql.schema_linking import resolution as shared

    assert hasattr(linker, "SchemaLinker")
    # Импортированные функции должны быть идентичны функциям из общего модуля.
    assert linker._resolve_table_name is shared._resolve_table_name
    assert linker._resolve_column_name is shared._resolve_column_name
    assert linker._table_exists_in_schema is shared._table_exists_in_schema
    assert linker._column_exists_in_table is shared._column_exists_in_table
    assert linker._get_column_meta is shared._get_column_meta


def test_join_validator_class_exported():
    from custom_tools.text_to_sql.schema_linking import JoinValidator
    from custom_tools.text_to_sql.schema_linking.join_validation import (
        JoinValidator as JV2,
    )

    assert JoinValidator is JV2


def test_decomposed_linkers_exported():
    """EPIC 8.2: heuristic / llm linkers доступны как public API package."""
    from custom_tools.text_to_sql.schema_linking import HeuristicLinker, LLMLinker

    assert callable(HeuristicLinker)
    assert callable(LLMLinker)


def test_resolve_table_name_basic_behaviour():
    """Лёгкая проверка чистоты функции (без классa) и контракта."""
    from custom_tools.text_to_sql.schema_linking.resolution import (
        _resolve_table_name,
    )

    schema = {"public.users": {"columns": {"id": {"type": "int"}}}}

    assert _resolve_table_name("public.users", schema) == "public.users"
    # case-insensitive по полному ключу
    assert _resolve_table_name("PUBLIC.USERS", schema) == "public.users"
    # короткое имя — единственный матч
    assert _resolve_table_name("users", schema) == "public.users"
    # отсутствует
    assert _resolve_table_name("unknown", schema) is None
    # пустая строка
    assert _resolve_table_name("", schema) is None


def test_resolve_column_name_basic_behaviour():
    from custom_tools.text_to_sql.schema_linking.resolution import (
        _resolve_column_name,
    )

    schema = {
        "public.users": {
            "columns": {
                "id": {"type": "int"},
                "Name": {"type": "text"},
            }
        }
    }

    assert _resolve_column_name("id", "public.users", schema) == "id"
    # case-insensitive
    assert _resolve_column_name("NAME", "public.users", schema) == "Name"
    # отсутствует
    assert _resolve_column_name("missing", "public.users", schema) is None
    # таблица не разрезолвлена — функция требует уже резолвленное имя
    assert _resolve_column_name("id", "users", schema) is None
