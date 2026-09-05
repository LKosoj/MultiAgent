"""Тесты DSN-scoped Text-to-SQL профиля (W1-1.2, часть A).

Пользовательское требование: «профиль должен собираться под конкретный DSN
в разрезе схемы БД! Не может быть универсальных профилей». Покрывают:
  * два разных DSN → два разных файла профиля, без смешивания;
  * отсутствие файла → ``DsnProfile.empty()`` (без fallback на именованные
    профили);
  * ``schema_namespace_version`` mismatch → warning / ``RuntimeError`` (strict);
  * чужой ``dsn_fingerprint`` → ``ValueError``;
  * невалидный yaml / неизвестные ключи / неверные типы → ``ValueError``;
  * ``build_schema_linking_prompt(..., dsn=...)`` — приоритет DSN-профиля;
  * ``_compute_env_fingerprint`` реагирует на изменение файла профиля;
  * scaffold-скрипт создаёт валидный файл, ``--force`` семантика.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
import yaml

import custom_tools.text_to_sql.dsn_profile as dp
from custom_tools.text_to_sql.prompts import build_schema_linking_prompt
from custom_tools.text_to_sql.schema_cache import _compute_env_fingerprint

DSN_A = "postgresql://host-a:5432/db_a"
DSN_B = "postgresql://host-b:5432/db_b"


@pytest.fixture(autouse=True)
def _isolated_sqlrag_root(tmp_path, monkeypatch):
    """Изолирует sqlrag-корень: тесты не должны трогать реальный sqlrag/ проекта."""
    monkeypatch.setattr(dp, "get_repo_root", lambda: tmp_path)
    dp.reset_cache()
    yield
    dp.reset_cache()


def _minimal_mapping(**overrides) -> dict:
    mapping = {
        "version": 1,
        "dsn_fingerprint": None,
        "schema_namespace_version": None,
        "captured_at": None,
        "glossary": [],
        "aliases": {},
        "type_hints": {},
        "metric_hints": {
            "priority_id_columns": [],
            "low_priority_name_columns": [],
            "prefer_id_over_name_rules": [],
            "significant_columns": {
                "high_priority_exact": [],
                "high_priority_compound": [],
                "critical_description_keywords": [],
            },
        },
        "nlu_hints": {},
        "few_shots_ref": None,
    }
    mapping.update(overrides)
    return mapping


def _write_profile(dsn: str, mapping: dict) -> Path:
    path = dp.dsn_profile_path(dsn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(mapping, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# 1. Два DSN → два файла, без смешивания.
# ---------------------------------------------------------------------------


def test_two_dsns_produce_separate_non_mixed_profiles():
    _write_profile(DSN_A, _minimal_mapping(aliases={"revenue": ["выручка"]}))
    _write_profile(DSN_B, _minimal_mapping(aliases={"population": ["население"]}))

    assert dp.dsn_profile_path(DSN_A) != dp.dsn_profile_path(DSN_B)

    profile_a = dp.load_dsn_profile(DSN_A)
    profile_b = dp.load_dsn_profile(DSN_B)

    assert profile_a.aliases == {"revenue": ("выручка",)}
    assert profile_b.aliases == {"population": ("население",)}
    assert "population" not in profile_a.aliases
    assert "revenue" not in profile_b.aliases


# ---------------------------------------------------------------------------
# 2. Нет файла → DsnProfile.empty(), без единого доменного термина.
# ---------------------------------------------------------------------------


def test_missing_file_returns_empty_profile_without_domain_terms():
    dsn = "postgresql://nowhere:5432/emptydb"
    profile = dp.load_dsn_profile(dsn)

    assert profile == dp.DsnProfile.empty()

    text = repr(profile)
    for token in ("oktmo", "territory_id", "revenue"):
        assert token not in text


# ---------------------------------------------------------------------------
# 3. schema_namespace_version mismatch → warning; strict → RuntimeError.
# ---------------------------------------------------------------------------


def test_schema_namespace_version_mismatch_warns_by_default(caplog):
    dsn = "postgresql://host-c:5432/db_c"
    _write_profile(dsn, _minimal_mapping(schema_namespace_version="a" * 64))

    with caplog.at_level(
        logging.WARNING, logger="custom_tools.text_to_sql.dsn_profile"
    ):
        profile = dp.load_dsn_profile(dsn, live_schema_fingerprint="b" * 64)

    assert profile.schema_namespace_version == "a" * 64
    assert any("stale" in record.getMessage() for record in caplog.records)


def test_schema_namespace_version_mismatch_strict_raises(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DSN_PROFILE_STRICT", "1")
    dsn = "postgresql://host-d:5432/db_d"
    _write_profile(dsn, _minimal_mapping(schema_namespace_version="a" * 64))

    with pytest.raises(RuntimeError):
        dp.load_dsn_profile(dsn, live_schema_fingerprint="b" * 64)


def test_schema_namespace_version_match_is_silent(caplog):
    dsn = "postgresql://host-c2:5432/db_c2"
    _write_profile(dsn, _minimal_mapping(schema_namespace_version="a" * 64))

    with caplog.at_level(
        logging.WARNING, logger="custom_tools.text_to_sql.dsn_profile"
    ):
        dp.load_dsn_profile(dsn, live_schema_fingerprint="a" * 64)

    assert caplog.records == []


# ---------------------------------------------------------------------------
# 4. Чужой dsn_fingerprint → ValueError.
# ---------------------------------------------------------------------------


def test_foreign_dsn_fingerprint_raises_value_error():
    dsn = "postgresql://host-e:5432/db_e"
    _write_profile(
        dsn,
        _minimal_mapping(dsn_fingerprint="postgresql://other-host:5432/other_db"),
    )

    with pytest.raises(ValueError, match="different DSN"):
        dp.load_dsn_profile(dsn)


# ---------------------------------------------------------------------------
# 5. Невалидный yaml / неизвестные ключи / неверные типы → ValueError.
# ---------------------------------------------------------------------------


def test_invalid_yaml_top_level_not_mapping_raises_value_error():
    dsn = "postgresql://host-f:5432/db_f"
    path = dp.dsn_profile_path(dsn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        dp.load_dsn_profile(dsn)


def test_unknown_top_level_key_raises_value_error():
    dsn = "postgresql://host-g:5432/db_g"
    _write_profile(dsn, _minimal_mapping(unexpected_key="oops"))

    with pytest.raises(ValueError, match="unexpected top-level keys"):
        dp.load_dsn_profile(dsn)


def test_invalid_version_type_raises_value_error():
    dsn = "postgresql://host-h:5432/db_h"
    _write_profile(dsn, _minimal_mapping(version="one"))

    with pytest.raises(ValueError, match="version"):
        dp.load_dsn_profile(dsn)


def test_malformed_yaml_syntax_raises_value_error():
    dsn = "postgresql://host-i:5432/db_i"
    path = dp.dsn_profile_path(dsn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version: [1, 2\n", encoding="utf-8")  # незакрытая скобка

    with pytest.raises(ValueError, match="invalid yaml"):
        dp.load_dsn_profile(dsn)


def test_unknown_glossary_entry_key_raises_value_error():
    dsn = "postgresql://host-i2:5432/db_i2"
    _write_profile(
        dsn,
        _minimal_mapping(
            glossary=[
                {"term": "x", "table": "t", "bogus_field": 1},
            ]
        ),
    )

    with pytest.raises(ValueError, match="unexpected keys"):
        dp.load_dsn_profile(dsn)


# ---------------------------------------------------------------------------
# 7. build_schema_linking_prompt(..., dsn=...) — приоритет DSN-профиля.
# ---------------------------------------------------------------------------


def _sample_inputs():
    entities = {"metrics": ["value"], "dimensions": ["region_code"], "filters": {}}
    schema_str = "schema.t1(region_code TEXT, region_label TEXT)"
    return entities, schema_str


def test_schema_linking_prompt_uses_dsn_profile_metric_hints_when_present():
    dsn = "postgresql://host-j:5432/db_j"
    _write_profile(
        dsn,
        _minimal_mapping(
            metric_hints={
                "priority_id_columns": ["region_code"],
                "low_priority_name_columns": ["region_label"],
                "prefer_id_over_name_rules": [
                    {"id_column": "region_code", "ignore_column": "region_label"}
                ],
                "significant_columns": {
                    "high_priority_exact": [],
                    "high_priority_compound": [],
                    "critical_description_keywords": [],
                },
            }
        ),
    )

    entities, schema_str = _sample_inputs()
    prompt = build_schema_linking_prompt(entities, schema_str, dsn=dsn)

    assert "ДОМЕННЫЕ ПРИМЕРЫ" in prompt
    assert "region_code" in prompt
    assert "region_label" in prompt


def test_schema_linking_prompt_without_dsn_is_unchanged():
    entities, schema_str = _sample_inputs()
    prompt = build_schema_linking_prompt(entities, schema_str)
    assert "ДОМЕННЫЕ ПРИМЕРЫ" not in prompt


def test_schema_linking_prompt_with_dsn_but_no_profile_file_is_unchanged():
    dsn = "postgresql://host-k2:5432/db_k2"  # файла профиля нет
    entities, schema_str = _sample_inputs()
    prompt = build_schema_linking_prompt(entities, schema_str, dsn=dsn)
    assert "ДОМЕННЫЕ ПРИМЕРЫ" not in prompt


# ---------------------------------------------------------------------------
# 8. _compute_env_fingerprint реагирует на изменение файла профиля.
# ---------------------------------------------------------------------------


def test_compute_env_fingerprint_changes_when_dsn_profile_file_changes():
    dsn = "postgresql://host-k:5432/db_k"
    before = _compute_env_fingerprint(dsn)

    _write_profile(dsn, _minimal_mapping())
    after_create = _compute_env_fingerprint(dsn)
    assert after_create != before

    path = dp.dsn_profile_path(dsn)
    path.write_text(
        yaml.safe_dump(
            _minimal_mapping(aliases={"x": ["y"]}), sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )
    after_modify = _compute_env_fingerprint(dsn)
    assert after_modify != after_create


# ---------------------------------------------------------------------------
# 9. Scaffold-скрипт: создаёт валидный файл; --force семантика.
# ---------------------------------------------------------------------------


def test_scaffold_script_creates_file_loadable_by_load_dsn_profile(tmp_path, monkeypatch):
    import scripts.text2sql_dsn_profile_scaffold as scaffold_mod
    from scripts.text2sql_dsn_profile_scaffold import main as scaffold_main

    # W1-1.2 blocker 2: раньше SchemaLoader внутри скрипта всегда использовал
    # РЕАЛЬНЫЙ корень репозитория (module-level REPO_ROOT, не затрагиваемый
    # monkeypatch'ем dp.get_repo_root выше) и autosave=True по умолчанию —
    # это оставляло sqlrag/<name>.json в реальном репозитории. Теперь скрипт
    # вызывает get_database_schema(..., autosave=False); дополнительно
    # подменяем REPO_ROOT скрипта на tmp_path, чтобы тест вообще не мог
    # задеть реальный sqlrag/, даже если бы autosave снова включили.
    monkeypatch.setattr(scaffold_mod, "REPO_ROOT", tmp_path)

    db_path = tmp_path / "case.sqlite"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    connection.commit()
    connection.close()

    dsn = f"sqlite:///{db_path}"
    out_path = dp.dsn_profile_path(dsn)
    sqlrag_dir = tmp_path / "sqlrag"

    exit_code = scaffold_main(["--dsn", dsn, "--out", str(out_path)])
    assert exit_code == 0
    assert out_path.is_file()

    loaded = dp.load_dsn_profile(dsn)
    assert loaded.version == 1
    assert loaded.dsn_fingerprint is not None
    assert loaded.schema_namespace_version is not None

    # autosave=False: единственный файл в sqlrag/ — это явно запрошенный
    # --out .profile.yaml, никакого побочного .json со схемой.
    stray_files = sorted(p.name for p in sqlrag_dir.iterdir() if p.is_file())
    assert stray_files == [out_path.name]

    # --force семантика: без --force — отказ (код 1, файл не тронут).
    refused = scaffold_main(["--dsn", dsn, "--out", str(out_path)])
    assert refused == 1

    forced = scaffold_main(["--dsn", dsn, "--out", str(out_path), "--force"])
    assert forced == 0

    dp.reset_cache()
    reloaded = dp.load_dsn_profile(dsn)
    assert reloaded.version == 1

    stray_files_after = sorted(p.name for p in sqlrag_dir.iterdir() if p.is_file())
    assert stray_files_after == [out_path.name]


# ---------------------------------------------------------------------------
# 10. build_schema_linking_prompt(..., schema_fingerprint=...) пробрасывает
#     live_schema_fingerprint в load_dsn_profile (блокер 4).
# ---------------------------------------------------------------------------


def test_schema_linking_prompt_schema_fingerprint_mismatch_warns(caplog):
    """Устаревший профиль (schema_fingerprint передан, но не совпадает) →
    warning. До фикса build_schema_linking_prompt никогда не передавал
    live_schema_fingerprint в load_dsn_profile, поэтому эта проверка была
    мёртвым кодом."""
    dsn = "postgresql://host-l:5432/db_l"
    _write_profile(dsn, _minimal_mapping(schema_namespace_version="a" * 64))

    entities, schema_str = _sample_inputs()
    with caplog.at_level(
        logging.WARNING, logger="custom_tools.text_to_sql.dsn_profile"
    ):
        build_schema_linking_prompt(
            entities, schema_str, dsn=dsn, schema_fingerprint="b" * 64
        )

    assert any("stale" in record.getMessage() for record in caplog.records)


def test_schema_linking_prompt_schema_fingerprint_mismatch_strict_raises(monkeypatch):
    """STRICT-режим: устаревший профиль на пути linker→prompt → RuntimeError."""
    monkeypatch.setenv("TEXT_TO_SQL_DSN_PROFILE_STRICT", "1")
    dsn = "postgresql://host-m:5432/db_m"
    _write_profile(dsn, _minimal_mapping(schema_namespace_version="a" * 64))

    entities, schema_str = _sample_inputs()
    with pytest.raises(RuntimeError):
        build_schema_linking_prompt(
            entities, schema_str, dsn=dsn, schema_fingerprint="b" * 64
        )


def test_schema_linking_prompt_schema_fingerprint_match_is_silent(caplog):
    dsn = "postgresql://host-n:5432/db_n"
    _write_profile(dsn, _minimal_mapping(schema_namespace_version="a" * 64))

    entities, schema_str = _sample_inputs()
    with caplog.at_level(
        logging.WARNING, logger="custom_tools.text_to_sql.dsn_profile"
    ):
        build_schema_linking_prompt(
            entities, schema_str, dsn=dsn, schema_fingerprint="a" * 64
        )

    assert caplog.records == []


# ---------------------------------------------------------------------------
# 11. Битый/чужой DSN-профиль не должен ронять весь schema-linking (блокер 5).
# ---------------------------------------------------------------------------


def test_schema_linking_prompt_degrades_on_foreign_dsn_profile(caplog):
    """ValueError из load_dsn_profile (чужой dsn_fingerprint) → warning +
    деградация на именованный профиль, а не падение всего построения промпта.
    DSN с credentials — проверяем, что пароль не попадает в warning."""
    dsn = "postgresql://user:s3cret-pass@host-o:5432/db_o"
    _write_profile(
        dsn,
        _minimal_mapping(dsn_fingerprint="postgresql://other-host:5432/other_db"),
    )

    entities, schema_str = _sample_inputs()
    with caplog.at_level(
        logging.WARNING, logger="custom_tools.text_to_sql.prompts"
    ):
        prompt = build_schema_linking_prompt(entities, schema_str, dsn=dsn)

    assert "ДОМЕННЫЕ ПРИМЕРЫ" not in prompt
    assert any(
        "failed to load profile" in record.getMessage() for record in caplog.records
    )
    for record in caplog.records:
        assert "s3cret-pass" not in record.getMessage()
    assert "s3cret-pass" not in prompt


# ---------------------------------------------------------------------------
# 12. Расширенный формат type_hints с весами в DSN-профиле не поддерживается
#     (блокер 7): молчаливая потеря весов → ValueError.
# ---------------------------------------------------------------------------


def test_type_hints_with_weights_raises_value_error():
    dsn = "postgresql://host-p:5432/db_p"
    _write_profile(
        dsn,
        _minimal_mapping(
            type_hints={
                "numeric": {
                    "tokens": ["сумма"],
                    "weight_solo": 5,
                    "weight_with_signal": 10,
                }
            }
        ),
    )

    with pytest.raises(ValueError, match="не поддерживаются"):
        dp.load_dsn_profile(dsn)


def test_type_hints_extended_format_without_weights_still_works():
    """{'tokens': [...]} без weight_* — не расширенный weighted-формат,
    молчаливой потери данных нет, парсинг проходит как обычно."""
    dsn = "postgresql://host-q:5432/db_q"
    _write_profile(
        dsn,
        _minimal_mapping(type_hints={"numeric": {"tokens": ["сумма"]}}),
    )

    profile = dp.load_dsn_profile(dsn)
    assert profile.type_hints == {"numeric": ("сумма",)}


# ---------------------------------------------------------------------------
# 13. Изоляция бенчмарка: sqlite-DSN бенчмарка без файла профиля → empty()
#     (блокер 9).
# ---------------------------------------------------------------------------


def test_load_dsn_profile_for_benchmark_sqlite_dsn_without_file_is_isolated(tmp_path):
    """Явная фиксация: бенчмарк-DSN не должен подцеплять никакие доменные
    подсказки по умолчанию — text-to-sql пайплайн не завязан на конкретный
    benchmark-кейс."""
    dsn = f"sqlite:///{tmp_path}/bench.sqlite"
    profile = dp.load_dsn_profile(dsn)
    assert profile == dp.DsnProfile.empty()
