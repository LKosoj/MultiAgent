import logging

import pytest

from custom_tools.text_to_sql.join_containment import (
    _clear_containment_cache,
    estimate_join_containment,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _clear_containment_cache()
    yield
    _clear_containment_cache()


class FakePlugin:
    """Фейк-плагин со счётчиками connect/close/execute_select.

    column_values: {(table, column): [values]} — данные DISTINCT-сэмпла.
    """

    def __init__(
        self,
        column_values=None,
        *,
        fail_connect=False,
        exec_error=None,
        exec_success=True,
    ):
        self.column_values = column_values or {}
        self.fail_connect = fail_connect
        self.exec_error = exec_error
        self.exec_success = exec_success
        self.connect_calls = 0
        self.close_calls = 0
        self.exec_calls = 0
        self.last_limit = None
        # Запоминаем (table, column, limit) для каждого build_distinct_values_query.
        self.distinct_calls = []

    def connect(self, dsn):
        self.connect_calls += 1
        if self.fail_connect:
            raise RuntimeError("connect boom")
        return object()

    def close(self, conn):
        self.close_calls += 1

    def build_distinct_values_query(self, table_name, column_name, limit):
        self.distinct_calls.append((table_name, column_name, limit))
        self.last_limit = limit
        return f"SELECT DISTINCT {column_name} FROM {table_name} LIMIT {limit}"

    def execute_select(self, conn, sql, row_limit=None):
        self.exec_calls += 1
        if self.exec_error is not None:
            raise self.exec_error
        if not self.exec_success:
            return {"success": False, "error_message": "boom"}
        # Определяем какие данные вернуть по тексту SQL (table/column в нём есть).
        for (table, column), values in self.column_values.items():
            if f"FROM {table}" in sql and column in sql:
                return {"success": True, "data": [(v,) for v in values]}
        return {"success": True, "data": []}


class PluginNoBuildDistinct:
    """Плагин без build_distinct_values_query."""

    def __init__(self):
        self.connect_calls = 0
        self.close_calls = 0
        self.exec_calls = 0

    def connect(self, dsn):
        self.connect_calls += 1
        return object()

    def close(self, conn):
        self.close_calls += 1

    def execute_select(self, conn, sql, row_limit=None):
        self.exec_calls += 1
        return {"success": True, "data": []}


DSN = "postgresql://app:s3cr3t@db.example.com:5432/sales"


def _di(plugin):
    """Возвращает get_plugin-замыкание для DI-хука."""
    return lambda dsn: plugin


# --- Метрика ---------------------------------------------------------------


def test_containment_full_subset():
    # a ⊆ b → containment == 1.0
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1", "2", "3"],
            ("b", "col_b"): ["1", "2", "3", "4", "5"],
        }
    )
    result = estimate_join_containment(
        DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
    )
    assert result == 1.0


def test_containment_no_overlap():
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1", "2", "3"],
            ("b", "col_b"): ["x", "y", "z"],
        }
    )
    result = estimate_join_containment(
        DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
    )
    assert result == 0.0


def test_containment_partial_half():
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1", "2", "3", "4"],
            ("b", "col_b"): ["1", "2", "999"],
        }
    )
    result = estimate_join_containment(
        DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
    )
    assert result == 0.5


def test_empty_a_returns_none():
    # Знаменатель 0 → None (неопределимо, НЕ 0.0).
    plugin = FakePlugin(
        {
            ("a", "col_a"): [],
            ("b", "col_b"): ["1", "2"],
        }
    )
    result = estimate_join_containment(
        DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
    )
    assert result is None


def test_empty_b_returns_zero():
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1", "2"],
            ("b", "col_b"): [],
        }
    )
    result = estimate_join_containment(
        DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
    )
    assert result == 0.0


# --- fail-open -------------------------------------------------------------


def test_fail_connect_returns_none_and_warning(caplog):
    plugin = FakePlugin({("a", "col_a"): ["1"]}, fail_connect=True)
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.join_containment"):
        result = estimate_join_containment(
            DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
        )
    assert result is None
    assert "fail-open" in caplog.text
    # error-уровня быть не должно.
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_fail_exec_timeout_returns_none_and_warning(caplog):
    plugin = FakePlugin({("a", "col_a"): ["1"]}, exec_error=TimeoutError("slow"))
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.join_containment"):
        result = estimate_join_containment(
            DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
        )
    assert result is None
    assert "TimeoutError" in caplog.text
    # conn должен быть закрыт даже при ошибке execute_select.
    assert plugin.close_calls == 1


def test_plugin_without_build_distinct_returns_none_and_warning(caplog):
    plugin = PluginNoBuildDistinct()
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.join_containment"):
        result = estimate_join_containment(
            DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
        )
    assert result is None
    assert "fail-open" in caplog.text
    # connect случился, close в finally тоже.
    assert plugin.close_calls == 1


def test_execute_select_success_false_returns_none_and_warning(caplog):
    plugin = FakePlugin({("a", "col_a"): ["1"]}, exec_success=False)
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.join_containment"):
        result = estimate_join_containment(
            DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
        )
    assert result is None
    assert "fail-open" in caplog.text


# --- Кэш -------------------------------------------------------------------


def test_cache_hit_does_not_re_execute():
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1", "2"],
            ("b", "col_b"): ["1", "2"],
        }
    )
    r1 = estimate_join_containment(DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin))
    calls_after_first = plugin.exec_calls
    r2 = estimate_join_containment(DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin))
    assert r1 == r2 == 1.0
    # 2 exec на первый вызов (2 сэмпла), на второй — кэш-хит, не растёт.
    assert calls_after_first == 2
    assert plugin.exec_calls == 2


def test_distinct_cache_keys_execute_independently():
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1", "2"],
            ("b", "col_b"): ["1", "2"],
            ("c", "col_c"): ["1"],
            ("d", "col_d"): ["9"],
        }
    )
    estimate_join_containment(DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin))
    estimate_join_containment(DSN, "c", "col_c", "d", "col_d", get_plugin=_di(plugin))
    # Разные ключи → оба исполнились: 2 + 2 = 4 exec.
    assert plugin.exec_calls == 4


def test_negative_cache_for_none_does_not_reconnect():
    plugin = FakePlugin({("a", "col_a"): ["1"]}, fail_connect=True)
    r1 = estimate_join_containment(DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin))
    connects_after_first = plugin.connect_calls
    r2 = estimate_join_containment(DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin))
    assert r1 is None and r2 is None
    # None закэширован (negative-cache) → 2-й вызов не коннектится.
    assert connects_after_first == 1
    assert plugin.connect_calls == 1


def test_ttl_zero_disables_cache(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_CONTAINMENT_CACHE_TTL_S", "0")
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1", "2"],
            ("b", "col_b"): ["1", "2"],
        }
    )
    estimate_join_containment(DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin))
    estimate_join_containment(DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin))
    # TTL=0 → кэш off → оба вызова исполнились: 2 + 2 = 4 exec.
    assert plugin.exec_calls == 4


# --- sample_size прокидывается --------------------------------------------


def test_sample_size_propagated_to_build_distinct():
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1"],
            ("b", "col_b"): ["1"],
        }
    )
    estimate_join_containment(
        DSN, "a", "col_a", "b", "col_b", sample_size=37, get_plugin=_di(plugin)
    )
    # Оба build_distinct_values_query получили limit=37.
    assert plugin.distinct_calls == [("a", "col_a", 37), ("b", "col_b", 37)]


def test_env_sample_size_used_when_none(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_CONTAINMENT_SAMPLE", "55")
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1"],
            ("b", "col_b"): ["1"],
        }
    )
    estimate_join_containment(DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin))
    assert all(limit == 55 for (_, _, limit) in plugin.distinct_calls)


# --- DSN-маскинг -----------------------------------------------------------


def test_dsn_password_not_in_caplog(caplog):
    plugin = FakePlugin({("a", "col_a"): ["1"]}, fail_connect=True)
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.join_containment"):
        estimate_join_containment(
            DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
        )
    assert "s3cr3t" not in caplog.text
    assert "app:s3cr3t" not in caplog.text


# --- DI и monkeypatch оба работают ----------------------------------------


def test_di_get_plugin_works():
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1", "2"],
            ("b", "col_b"): ["1"],
        }
    )
    result = estimate_join_containment(
        DSN, "a", "col_a", "b", "col_b", get_plugin=_di(plugin)
    )
    assert result == 0.5


def test_monkeypatch_get_plugin_works(monkeypatch):
    plugin = FakePlugin(
        {
            ("a", "col_a"): ["1", "2"],
            ("b", "col_b"): ["1"],
        }
    )
    monkeypatch.setattr("db_plugins.get_plugin", lambda dsn: plugin)
    # get_plugin=None → ленивый импорт из db_plugins → попадает в monkeypatch.
    result = estimate_join_containment(DSN, "a", "col_a", "b", "col_b")
    assert result == 0.5
