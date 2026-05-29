"""Тесты EPIC 3 для блока schema_enricher.

Покрытие:

* 3.8  — sample-данные маскируются helper'ом ``core/_pii.py::pii_masking``
         ДО отправки в LLM.
* 3.9  — отсутствие ``estimate_row_count`` → fail-fast (без magic 1_000_000).
* 3.10 — разделение retryable (network/timeout) и fatal (validation/parse)
         ошибок в outer-обработчике enrichment'а.
* 3.11 — узкий catch fatal-классов вместо ``except (json.JSONDecodeError, Exception)``.
* 3.28 — top-level import call_openai_api защищён try/except, доступен
         для monkeypatch.
"""
import importlib
import json

import pytest

from custom_tools.text_to_sql import schema_enricher as enricher_module
from custom_tools.text_to_sql.schema_enricher import (
    DBSampleFailed,
    SchemaEnricher,
    _FATAL_ENRICHMENT_EXCEPTIONS,
    _is_retryable_error,
)


# ---------------------------------------------------------------------------
# 3.28: relative/safe imports работают и monkeypatch-friendly
# ---------------------------------------------------------------------------


def test_relative_imports_work_in_subpackage():
    """schema_enricher и sql_generator должны импортироваться при любом cwd.

    Top-level ``from utils import call_openai_api`` теперь обёрнут в
    try/except (3.28), поэтому даже если корневой ``utils`` недоступен,
    модуль остаётся импортируемым (call_openai_api = None) и его атрибут
    можно подменить через ``monkeypatch.setattr``.
    """
    # Перезагрузка не должна падать.
    importlib.reload(enricher_module)

    # call_openai_api должен быть атрибутом модуля (None или callable).
    assert hasattr(enricher_module, "call_openai_api")
    assert enricher_module.call_openai_api is None or callable(
        enricher_module.call_openai_api
    )

    # Относительные импорты внутри пакета должны работать.
    from custom_tools.text_to_sql.utils import (  # noqa: F401
        get_table_columns,
        parse_llm_json_response,
    )
    from custom_tools.text_to_sql.core._pii import pii_masking  # noqa: F401

    # sql_generator — аналогично 3.28.
    from custom_tools.text_to_sql import sql_generator as sql_gen_module

    importlib.reload(sql_gen_module)
    assert hasattr(sql_gen_module, "call_openai_api")


# ---------------------------------------------------------------------------
# 3.8: PII маскирование sample-данных перед отправкой в LLM
# ---------------------------------------------------------------------------


def test_sample_data_pii_masked_before_llm(monkeypatch):
    """Sample-данные должны проходить через pii_masking перед попаданием в prompt.

    Имитируем LLM, перехватывая prompt; проверяем, что в prompt'е
    отсутствуют исходные PII-значения (есть masked-маркер ``***``).
    """
    monkeypatch.setenv("SCHEMA_DESCRIBE_WITH_LLM", "1")
    monkeypatch.setenv("PII_MASKING_ENABLED", "1")

    # Захватываем prompt + возвращаем валидный JSON-ответ LLM.
    captured = {}

    def fake_call_openai_api(prompt, system_prompt=None, max_tokens=None,
                              response_format=None, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({
            "descriptions": {"users": {"email": "Email пользователя"}},
            "table_description": {"users": "Пользователи"},
        })

    monkeypatch.setattr(enricher_module, "call_openai_api", fake_call_openai_api)

    # Маскируем pii_masking так, чтобы детерминированно подменять колонку email.
    def fake_pii_masking(data, columns_to_mask, column_names=None):
        # Эмулируем AUTO-detection: видим колонку email → маскируем.
        idx = column_names.index("email") if column_names and "email" in column_names else None
        if idx is None:
            return {"masked_data": data}
        masked = []
        for row in data:
            new_row = list(row)
            new_row[idx] = "***masked"
            masked.append(new_row)
        return {"masked_data": masked}

    monkeypatch.setattr(enricher_module, "pii_masking", fake_pii_masking)

    raw_sample_data = [
        {"id": 1, "email": "alice@example.com"},
        {"id": 2, "email": "bob@example.com"},
    ]

    enricher = SchemaEnricher()
    # Подменяем get_table_sample_data, чтобы не лезть в БД.
    monkeypatch.setattr(
        enricher,
        "get_table_sample_data",
        lambda table_name: {
            "sample_rows": raw_sample_data,
            "column_stats": {},
            "fk_previews": {},
        },
    )

    schema_obj = {
        "users": {
            "description": "",
            "columns": {
                "id": {"type": "INTEGER", "description": ""},
                "email": {"type": "VARCHAR", "description": ""},
            },
        }
    }
    enricher.enrich_descriptions_with_llm(schema_obj)

    assert "prompt" in captured, "LLM не был вызван"
    prompt = captured["prompt"]
    # Сырые email-значения НЕ должны попасть в prompt — должна быть маска.
    assert "alice@example.com" not in prompt
    assert "bob@example.com" not in prompt
    assert "***masked" in prompt


def test_sample_data_pii_masking_disabled_passes_through(monkeypatch):
    """Когда PII_MASKING_ENABLED=0, helper возвращает данные без изменений.

    Проверяем, что в этом режиме данные доходят до LLM как есть (это
    штатное поведение pii_masking — не наш силовой fallback).
    """
    monkeypatch.setenv("SCHEMA_DESCRIBE_WITH_LLM", "1")
    monkeypatch.setenv("PII_MASKING_ENABLED", "0")

    captured = {}

    def fake_call_openai_api(prompt, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({"descriptions": {"users": {}}})

    monkeypatch.setattr(enricher_module, "call_openai_api", fake_call_openai_api)

    enricher = SchemaEnricher()
    monkeypatch.setattr(
        enricher,
        "get_table_sample_data",
        lambda table_name: {
            "sample_rows": [{"name": "Alice"}],
            "column_stats": {},
            "fk_previews": {},
        },
    )

    schema_obj = {
        "users": {
            "description": "",
            "columns": {"name": {"type": "VARCHAR", "description": ""}},
        }
    }
    enricher.enrich_descriptions_with_llm(schema_obj)

    # При выключенном маскировании оригинальные данные должны дойти до prompt.
    assert "Alice" in captured["prompt"]


def test_mask_sample_data_pii_empty_inputs():
    """Пустые входы не должны падать и не вызывают pii_masking."""
    enricher = SchemaEnricher()
    assert enricher._mask_sample_data_pii([]) == []
    assert enricher._mask_sample_data_pii(None) is None


def test_mask_sample_data_pii_rejects_non_dict_rows():
    """Нестандартная форма sample_data → TypeError, без silent fallback."""
    enricher = SchemaEnricher()
    with pytest.raises(TypeError):
        enricher._mask_sample_data_pii([["raw", "list", "row"]])


# ---------------------------------------------------------------------------
# 3.9: fail-fast при отсутствии estimate_row_count
# ---------------------------------------------------------------------------


class _PluginWithoutEstimate:
    """Плагин без метода estimate_row_count — должен вызвать fail-fast."""

    def connect(self, dsn):
        return object()

    def close(self, conn):
        pass

    def get_basic_column_stats(self, conn, table_name):
        return {}

    def sample_rows_smart(self, conn, table_name, strategy, max_rows):
        return {"columns": [], "data": []}


def test_no_row_count_fallback_fails_fast(monkeypatch):
    """Плагин без estimate_row_count → DBSampleFailed (W2-T2).

    EPIC 3.9 заменил magic-число 1_000_000 на AttributeError, но широкий
    ``except Exception → return default_result`` всё ещё глушил его.
    W2-T2: AttributeError оборачивается в DBSampleFailed и пробрасывается.

    NOTE: используем ``enricher_module.DBSampleFailed`` (а не импорт из
    шапки), потому что ``test_relative_imports_work_in_subpackage`` делает
    ``importlib.reload(enricher_module)`` — после reload-а на module-level
    пересоздаётся НОВЫЙ класс, и pytest.raises с импортом из шапки
    получает stale-объект и не матчится.
    """
    monkeypatch.setenv("DB_DSN", "sqlite:///dummy.db")

    plugin = _PluginWithoutEstimate()
    monkeypatch.setattr(
        "db_plugins.get_plugin",
        lambda dsn: plugin,
    )

    enricher = enricher_module.SchemaEnricher()
    with pytest.raises(enricher_module.DBSampleFailed) as exc_info:
        enricher.get_table_sample_data("any_table")

    # Контекст ошибки должен включать имя таблицы (caller-логи) и
    # сохранять оригинальную причину в ``__cause__`` (диагностика).
    assert exc_info.value.table_name == "any_table"
    assert isinstance(exc_info.value.__cause__, AttributeError)
    # Magic-число 1_000_000 не должно появиться в сообщении (это было
    # silent-fallback значение до 3.9).
    assert "1000000" not in str(exc_info.value)
    assert "1_000_000" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3.10: разделение retryable vs fatal ошибок
# ---------------------------------------------------------------------------


def test_enrichment_retryable_vs_fatal_errors():
    """Helper ``_is_retryable_error`` корректно классифицирует ошибки."""
    # Fatal: validation/parse — НЕ retryable.
    assert not _is_retryable_error(json.JSONDecodeError("bad", "doc", 0))
    assert not _is_retryable_error(ValueError("invalid"))
    assert not _is_retryable_error(KeyError("missing"))
    assert not _is_retryable_error(TypeError("wrong type"))

    # Retryable: транзиентные сетевые/timeout ошибки.
    assert _is_retryable_error(Exception("Read timeout"))
    assert _is_retryable_error(Exception("connection refused"))
    assert _is_retryable_error(Exception("Network is unreachable"))
    assert _is_retryable_error(Exception("503 Service Unavailable"))
    assert _is_retryable_error(Exception("504 Gateway Timeout"))

    # Неклассифицированное — НЕ помечается retryable (будет fail-fast).
    assert not _is_retryable_error(Exception("internal logic error"))


def test_fatal_error_fails_fast(monkeypatch):
    """ValueError из parse_llm_json_response → fail-fast пробрасывается.

    W2-T6: parse_llm_json_response теперь оборачивает все парсинг-ошибки
    в ``ValueError("LLM JSON parse failed: …")`` (без мутаций исходника).
    SchemaEnricher уже ловит ValueError как fatal → fail-fast сохраняется.
    """
    monkeypatch.setenv("SCHEMA_DESCRIBE_WITH_LLM", "1")
    # PII off — чтобы не дёргать LLM на PII auto-detection.
    monkeypatch.setenv("PII_MASKING_ENABLED", "0")

    # LLM возвращает невалидный JSON → ValueError при парсинге (W2-T6: wrap).
    monkeypatch.setattr(
        enricher_module,
        "call_openai_api",
        lambda **kwargs: "this is not json at all",
    )

    enricher = SchemaEnricher()
    monkeypatch.setattr(
        enricher,
        "get_table_sample_data",
        lambda table_name: {"sample_rows": [], "column_stats": {}, "fk_previews": {}},
    )

    schema_obj = {
        "users": {
            "description": "",
            "columns": {"name": {"type": "VARCHAR", "description": ""}},
        }
    }
    # Раньше silent swallowed; теперь — fail-fast.
    with pytest.raises(ValueError, match="LLM JSON parse failed"):
        enricher.enrich_descriptions_with_llm(schema_obj)


def test_retryable_error_after_retries_exhausted_also_fails_fast(monkeypatch):
    """call_openai_api поднял network-like Exception (retries исчерпаны) → fail-fast."""
    monkeypatch.setenv("SCHEMA_DESCRIBE_WITH_LLM", "1")
    monkeypatch.setenv("PII_MASKING_ENABLED", "0")

    def raising(**kwargs):
        raise Exception("Connection timeout after 3 retries")

    monkeypatch.setattr(enricher_module, "call_openai_api", raising)

    enricher = SchemaEnricher()
    monkeypatch.setattr(
        enricher,
        "get_table_sample_data",
        lambda table_name: {"sample_rows": [], "column_stats": {}, "fk_previews": {}},
    )

    schema_obj = {
        "users": {
            "description": "",
            "columns": {"name": {"type": "VARCHAR", "description": ""}},
        }
    }
    # Network/timeout, прошедший наружу — это retry-budget исчерпан.
    # По AGENTS.md silent swallow запрещён → fail-fast.
    with pytest.raises(Exception, match="Connection timeout"):
        enricher.enrich_descriptions_with_llm(schema_obj)


# ---------------------------------------------------------------------------
# 3.11: узкий exception handler (вместо except (json.JSONDecodeError, Exception))
# ---------------------------------------------------------------------------


def test_narrow_exception_handlers():
    """Inner-обработчик enrich_descriptions_with_llm не должен ловить
    arbitrary Exception под маской JSONDecodeError tuple.

    Раньше ``except (json.JSONDecodeError, Exception) as e`` был эквивалентен
    ``except Exception`` и приводил к silent fallback'у. После 3.11 ветка
    разделена на _FATAL_ENRICHMENT_EXCEPTIONS (re-raise) и retryable
    (re-raise).
    """
    # Проверяем декларативно — кортеж fatal-исключений узок и не содержит Exception.
    assert Exception not in _FATAL_ENRICHMENT_EXCEPTIONS
    assert json.JSONDecodeError in _FATAL_ENRICHMENT_EXCEPTIONS
    assert ValueError in _FATAL_ENRICHMENT_EXCEPTIONS
    assert KeyError in _FATAL_ENRICHMENT_EXCEPTIONS
    assert TypeError in _FATAL_ENRICHMENT_EXCEPTIONS
    # И ничего лишнего (например, BaseException/RuntimeError) не подмешано.
    assert all(
        isinstance(cls, type) and issubclass(cls, Exception)
        for cls in _FATAL_ENRICHMENT_EXCEPTIONS
    )
    # RuntimeError (используется как retryable-ловушка для call_openai_api=None)
    # НЕ должен считаться fatal в этой классификации.
    assert RuntimeError not in _FATAL_ENRICHMENT_EXCEPTIONS


# ---------------------------------------------------------------------------
# W2-T2 — DBSampleFailed: get_table_sample_data fail-fast с маскированным DSN
# ---------------------------------------------------------------------------


class _BrokenPlugin:
    """Плагин, у которого падает connect — типичный «БД легла» сценарий."""

    def connect(self, dsn):
        raise ConnectionError("connection refused")

    def close(self, conn):  # pragma: no cover — connect не дошёл
        pass


def test_get_sample_data_raises_db_sample_failed_on_connect_error(monkeypatch):
    """W2-T2: connect-ошибка → DBSampleFailed, а не silent default_result.

    DSN ВСЕГДА маскируется через ``utils.mask_dsn``: даже если драйвер
    кладёт user/pass в текст ошибки, в сообщении DBSampleFailed их не
    будет (AGENTS.md: PII в коде ошибок не хранить).
    """
    # DSN с credentials — это main-сценарий: проверяем маскирование.
    monkeypatch.setenv("DB_DSN", "postgresql://alice:secret123@db.host:5432/app")
    monkeypatch.setattr(
        "db_plugins.get_plugin",
        lambda dsn: _BrokenPlugin(),
    )

    # enricher_module.DBSampleFailed (а не импорт из шапки), потому что
    # test_relative_imports делает importlib.reload и пересоздаёт класс.
    enricher = enricher_module.SchemaEnricher()
    with pytest.raises(enricher_module.DBSampleFailed) as exc:
        enricher.get_table_sample_data("orders")

    err = exc.value
    assert err.table_name == "orders"
    # Оригинал в __cause__ сохраняется для диагностики.
    assert isinstance(err.__cause__, ConnectionError)
    # PII (пароль) НЕ должен попасть в сообщение, даже если бы он
    # утёк через текст исключения драйвера.
    text = str(err)
    assert "secret123" not in text


def test_get_sample_data_raises_when_dsn_missing(monkeypatch):
    """W2-T2 финал-ревью: отсутствие DB_DSN → DBSampleFailed (а не default).

    Раньше missing DSN молча возвращал default_result — это был silent
    fallback, запрещённый AGENTS.md (caller получал пустой sample и
    LLM писал бессмысленные описания, не отличая «БД не настроена» от
    «таблица пустая»). Теперь — fail-fast: caller должен явно поймать
    DBSampleFailed и принять решение (skip/abort).
    """
    monkeypatch.delenv("DB_DSN", raising=False)

    enricher = SchemaEnricher()
    with pytest.raises(enricher_module.DBSampleFailed) as exc:
        enricher.get_table_sample_data("any_table")
    assert exc.value.table_name == "any_table"
    assert "DB_DSN" in str(exc.value)


def test_get_sample_data_does_not_leak_dsn_via_driver_error(monkeypatch):
    """W2-T2: даже если драйвер кладёт DSN в текст ошибки, mask_dsn чистит.

    Сценарий: некоторые драйверы оборачивают DSN в текст исключения
    («failed connecting to postgresql://user:pass@…»). Helper
    ``mask_dsn`` ловит и эту утечку, не только credentials отдельно.
    """
    monkeypatch.setenv("DB_DSN", "postgresql://eve:topsecret@db.host:5432/app")

    class _LeakingPlugin:
        def connect(self, dsn):
            # Имитация драйвера, кладущего DSN целиком в текст ошибки.
            raise RuntimeError(
                "failed connecting to postgresql://eve:topsecret@db.host:5432/app"
            )

        def close(self, conn):  # pragma: no cover
            pass

    monkeypatch.setattr("db_plugins.get_plugin", lambda dsn: _LeakingPlugin())

    # enricher_module.DBSampleFailed — см. комментарий в первом тесте W2-T2.
    enricher = enricher_module.SchemaEnricher()
    with pytest.raises(enricher_module.DBSampleFailed) as exc:
        enricher.get_table_sample_data("orders")

    text = str(exc.value)
    # Пароль не должен попасть в сообщение.
    assert "topsecret" not in text
    assert "eve" not in text
    # Маска присутствует (mask_dsn заменяет на ***:***@).
    assert "***" in text


# ---------------------------------------------------------------------------
# #13: get_table_sample_data принимает dsn с приоритетом над runtime-context
# ---------------------------------------------------------------------------


class _CapturingPlugin:
    """Плагин, захватывающий DSN переданный в get_plugin."""

    def __init__(self):
        self.captured_dsn = None

    def connect(self, dsn):
        self.captured_dsn = dsn
        # Кидаем управляемую ошибку — нас интересует только DSN.
        raise ConnectionError("stop here")

    def close(self, conn):  # pragma: no cover
        pass


def test_get_sample_data_uses_runtime_context_dsn_when_env_absent(monkeypatch):
    """#13: при dsn=None (дефолт) и отсутствующем DB_DSN используется runtime-context DSN.

    Проверяем, что get_plugin вызывается именно с runtime-DSN, а не с None/пустой строкой.
    """
    monkeypatch.delenv("DB_DSN", raising=False)

    runtime_dsn = "postgresql://runtime/db"

    # Подменяем get_runtime_context_dsn в модуле enricher
    monkeypatch.setattr(
        enricher_module,
        "get_runtime_context_dsn",
        lambda: runtime_dsn,
    )

    plugin = _CapturingPlugin()
    monkeypatch.setattr("db_plugins.get_plugin", lambda dsn: plugin)

    enricher = enricher_module.SchemaEnricher()
    with pytest.raises(enricher_module.DBSampleFailed):
        enricher.get_table_sample_data("orders")

    # get_plugin должен был получить runtime DSN
    assert plugin.captured_dsn == runtime_dsn


def test_get_sample_data_explicit_dsn_has_highest_priority(monkeypatch):
    """#13: явно переданный dsn имеет наивысший приоритет над runtime-context и env.

    Даже если и env, и runtime-context заданы — должен использоваться явный аргумент.
    """
    monkeypatch.setenv("DB_DSN", "postgresql://from-env/db")
    monkeypatch.setattr(
        enricher_module,
        "get_runtime_context_dsn",
        lambda: "postgresql://from-runtime/db",
    )

    plugin = _CapturingPlugin()
    monkeypatch.setattr("db_plugins.get_plugin", lambda dsn: plugin)

    explicit_dsn = "postgresql://explicit/db"
    enricher = enricher_module.SchemaEnricher()
    with pytest.raises(enricher_module.DBSampleFailed):
        enricher.get_table_sample_data("orders", dsn=explicit_dsn)

    # Явный аргумент должен выиграть
    assert plugin.captured_dsn == explicit_dsn
