"""Тесты для общего ``YamlConfigLoader[T]`` (EPIC 8.7).

Покрывают:
  * загрузка с дефолтным путём;
  * override через env-переменную;
  * fail-fast при отсутствии файла (сообщение содержит env-var и путь);
  * fail-fast при некорректном top-level (не mapping);
  * cache hit (второй вызов не читает диск);
  * reset_cache() перечитывает файл;
  * profile-aware cache (cache key = (path, extra)).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from custom_tools.text_to_sql._yaml_config_loader import YamlConfigLoader


class _Holder:
    """Простой контейнер для парсинга в тестах."""

    __slots__ = ("raw", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.raw = raw
        self.source_path = source_path


def _not_found_msg(path: Path, env: str) -> str:
    return f"missing at {path}; set {env}"


def _mapping_msg(path: Path) -> str:
    return f"top-level not mapping at {path}"


def _make_loader(
    *,
    default_path: Path,
    env_path_var: str = "TEST_YAML_LOADER_PATH",
    profile_extra=None,
) -> YamlConfigLoader[_Holder]:
    return YamlConfigLoader[_Holder](
        env_path_var=env_path_var,
        default_path=default_path,
        parser=lambda raw, src: _Holder(raw, src),
        not_found_message=_not_found_msg,
        mapping_error_message=_mapping_msg,
        profile_extra=profile_extra,
    )


def test_loads_from_default_path(tmp_path, monkeypatch):
    """Если env-переменная не задана — берётся ``default_path``."""
    config_file = tmp_path / "default.yaml"
    config_file.write_text("foo: bar\n", encoding="utf-8")
    monkeypatch.delenv("TEST_YAML_LOADER_PATH", raising=False)

    loader = _make_loader(default_path=config_file)
    result = loader.load()

    assert result.raw == {"foo": "bar"}
    assert result.source_path == str(config_file.resolve(strict=False))


def test_env_override(tmp_path, monkeypatch):
    """env-переменная имеет приоритет над ``default_path``."""
    default = tmp_path / "default.yaml"
    default.write_text("from: default\n", encoding="utf-8")
    override = tmp_path / "override.yaml"
    override.write_text("from: override\n", encoding="utf-8")

    monkeypatch.setenv("TEST_YAML_LOADER_PATH", str(override))
    loader = _make_loader(default_path=default)

    result = loader.load()
    assert result.raw == {"from": "override"}


def test_missing_file_fail_fast(tmp_path, monkeypatch):
    """Несуществующий файл → ``FileNotFoundError`` с env-var и путём."""
    missing = tmp_path / "absent.yaml"
    monkeypatch.delenv("TEST_YAML_LOADER_PATH", raising=False)
    loader = _make_loader(default_path=missing)

    with pytest.raises(FileNotFoundError) as exc_info:
        loader.load()

    msg = str(exc_info.value)
    assert "TEST_YAML_LOADER_PATH" in msg
    assert str(missing) in msg


def test_non_mapping_top_level_raises(tmp_path, monkeypatch):
    """Top-level не mapping → ``ValueError``."""
    config_file = tmp_path / "list.yaml"
    config_file.write_text("- 1\n- 2\n", encoding="utf-8")
    monkeypatch.delenv("TEST_YAML_LOADER_PATH", raising=False)

    loader = _make_loader(default_path=config_file)
    with pytest.raises(ValueError) as exc_info:
        loader.load()
    assert "top-level not mapping" in str(exc_info.value)


def test_cache_hit_does_not_reread(tmp_path, monkeypatch):
    """Повторный ``load()`` не читает файл (cache hit)."""
    config_file = tmp_path / "cached.yaml"
    config_file.write_text("k: v1\n", encoding="utf-8")
    monkeypatch.delenv("TEST_YAML_LOADER_PATH", raising=False)
    loader = _make_loader(default_path=config_file)

    first = loader.load()
    # Меняем файл на диске — кэш должен вернуть прежний результат.
    config_file.write_text("k: v2\n", encoding="utf-8")
    second = loader.load()

    assert first is second
    assert second.raw == {"k": "v1"}


def test_reset_cache_rereads_file(tmp_path, monkeypatch):
    """После ``reset_cache()`` файл читается заново."""
    config_file = tmp_path / "reset.yaml"
    config_file.write_text("k: v1\n", encoding="utf-8")
    monkeypatch.delenv("TEST_YAML_LOADER_PATH", raising=False)
    loader = _make_loader(default_path=config_file)

    first = loader.load()
    config_file.write_text("k: v2\n", encoding="utf-8")
    loader.reset_cache()
    second = loader.load()

    assert first is not second
    assert second.raw == {"k": "v2"}


def test_profile_extra_partitions_cache(tmp_path, monkeypatch):
    """profile_extra → отдельный кэш-слот на каждое значение профиля."""
    config_file = tmp_path / "profiled.yaml"
    config_file.write_text(
        "profiles:\n  default: {x: 1}\n  strict: {x: 2}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_YAML_LOADER_PATH", raising=False)

    active_profile = {"name": "default"}

    def parser(raw: Dict[str, Any], src: str) -> _Holder:
        # Парсим только активный профиль (по аналогии с safety_config).
        return _Holder(raw["profiles"][active_profile["name"]], src)

    loader = YamlConfigLoader[_Holder](
        env_path_var="TEST_YAML_LOADER_PATH",
        default_path=config_file,
        parser=parser,
        not_found_message=_not_found_msg,
        mapping_error_message=_mapping_msg,
        profile_extra=lambda: active_profile["name"],
    )

    default_result = loader.load()
    assert default_result.raw == {"x": 1}

    active_profile["name"] = "strict"
    strict_result = loader.load()
    assert strict_result.raw == {"x": 2}

    # Возврат к default — должен прийти из кэша (тот же объект).
    active_profile["name"] = "default"
    default_again = loader.load()
    assert default_again is default_result
