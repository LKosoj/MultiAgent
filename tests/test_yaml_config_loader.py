"""Тесты для общего ``YamlConfigLoader[T]`` (EPIC 8.7).

Покрывают:
  * загрузка с дефолтным путём;
  * override через env-переменную;
  * fail-fast при отсутствии файла (сообщение содержит env-var и путь);
  * fail-fast при некорректном top-level (не mapping);
  * cache hit (второй вызов не читает диск);
  * reset_cache() перечитывает файл;
  * profile-aware cache (cache key = (path, extra)).
  * object и SHA-version публикуются одним snapshot.
"""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict

import pytest

from custom_tools.text_to_sql._yaml_config_loader import (
    ConfigSnapshotVersion,
    YamlConfigLoader,
    get_active_yaml_config_versions,
    resolve_active_profile_name,
)


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


def test_snapshot_version_hashes_exact_bytes_and_reset_starts_new_generation(
    tmp_path,
    monkeypatch,
):
    config_file = tmp_path / "versioned.yaml"
    first_bytes = b"k: value\n# generation one\n"
    second_bytes = b"k: value\n# generation two\n"
    config_file.write_bytes(first_bytes)
    monkeypatch.delenv("TEST_YAML_LOADER_PATH", raising=False)
    loader = _make_loader(default_path=config_file)

    first = loader.load()
    first_version = loader.active_version()

    assert isinstance(first_version, ConfigSnapshotVersion)
    assert first_version.source_path == str(config_file.resolve(strict=False))
    assert first_version.profile is None
    assert first_version.content_sha256 == hashlib.sha256(first_bytes).hexdigest()
    active = get_active_yaml_config_versions()
    matching = [
        (key, value)
        for key, value in active.items()
        if value["source_path"] == first_version.source_path
    ]
    assert [value for _key, value in matching] == [first_version.to_mapping()]
    assert json.loads(json.dumps(active)) == active
    registry_key, version_mapping = matching[0]
    version_mapping["content_sha256"] = "changed-by-caller"
    assert (
        get_active_yaml_config_versions()[registry_key]["content_sha256"]
        != "changed-by-caller"
    )

    config_file.write_bytes(second_bytes)
    assert loader.load() is first
    assert loader.active_version() is first_version

    loader.reset_cache()
    assert loader.active_version() is None
    second = loader.load()
    second_version = loader.active_version()
    assert second is not first
    assert second_version is not None
    assert second_version.content_sha256 == hashlib.sha256(second_bytes).hexdigest()
    assert second_version != first_version


def test_reset_keeps_identical_snapshot_owned_by_second_loader(
    tmp_path,
    monkeypatch,
):
    config_file = tmp_path / "shared.yaml"
    config_file.write_text("k: value\n", encoding="utf-8")
    monkeypatch.delenv("TEST_YAML_LOADER_PATH", raising=False)
    first = _make_loader(default_path=config_file)
    second = _make_loader(default_path=config_file)

    first.load()
    second.load()
    version = first.active_version()
    assert version is not None
    assert second.active_version() == version
    registry_key = str(config_file.resolve(strict=False))

    first.reset_cache()
    assert get_active_yaml_config_versions()[registry_key] == version.to_mapping()
    first.reset_cache()
    assert get_active_yaml_config_versions()[registry_key] == version.to_mapping()

    second.reset_cache()
    assert registry_key not in get_active_yaml_config_versions()


def test_concurrent_loaders_publish_one_object_version_pair(
    tmp_path,
    monkeypatch,
):
    config_file = tmp_path / "concurrent.yaml"
    config_file.write_text("k: value\n", encoding="utf-8")
    monkeypatch.delenv("TEST_YAML_LOADER_PATH", raising=False)
    parse_calls = 0
    parse_lock = threading.Lock()
    start = threading.Barrier(8)

    def parser(raw: Dict[str, Any], source_path: str) -> _Holder:
        nonlocal parse_calls
        with parse_lock:
            parse_calls += 1
        return _Holder(raw, source_path)

    loader = YamlConfigLoader[_Holder](
        env_path_var="TEST_YAML_LOADER_PATH",
        default_path=config_file,
        parser=parser,
        not_found_message=_not_found_msg,
        mapping_error_message=_mapping_msg,
    )

    def load() -> tuple[_Holder, ConfigSnapshotVersion | None]:
        start.wait(timeout=5)
        result = loader.load()
        return result, loader.active_version()

    with ThreadPoolExecutor(max_workers=8) as executor:
        pairs = list(executor.map(lambda _index: load(), range(8)))

    assert parse_calls == 1
    assert len({id(config) for config, _version in pairs}) == 1
    assert len({version for _config, version in pairs}) == 1
    assert pairs[0][1] is not None


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
    default_version = loader.active_version()
    assert default_version is not None
    assert default_version.profile == "default"

    active_profile["name"] = "strict"
    strict_result = loader.load()
    assert strict_result.raw == {"x": 2}
    strict_version = loader.active_version()
    assert strict_version is not None
    assert strict_version.profile == "strict"
    assert strict_version.content_sha256 == default_version.content_sha256

    # Возврат к default — должен прийти из кэша (тот же объект).
    active_profile["name"] = "default"
    default_again = loader.load()
    assert default_again is default_result
    assert loader.active_version() is default_version


def test_resolve_active_profile_name_uses_umbrella_fallback(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_TEST_PROFILE", raising=False)
    monkeypatch.setenv("TEXT2SQL_PROFILE", "umbrella_value")

    assert (
        resolve_active_profile_name(
            None, env_var="TEXT_TO_SQL_TEST_PROFILE", default="default"
        )
        == "umbrella_value"
    )


def test_resolve_active_profile_name_precedence(monkeypatch):
    monkeypatch.setenv("TEXT2SQL_PROFILE", "umbrella")
    monkeypatch.setenv("TEXT_TO_SQL_TEST_PROFILE", "subsystem")

    assert (
        resolve_active_profile_name(
            "explicit", env_var="TEXT_TO_SQL_TEST_PROFILE", default="default"
        )
        == "explicit"
    )
    assert (
        resolve_active_profile_name(
            None, env_var="TEXT_TO_SQL_TEST_PROFILE", default="default"
        )
        == "subsystem"
    )


def test_resolve_active_profile_name_empty_values_fall_back(monkeypatch):
    monkeypatch.setenv("TEXT2SQL_PROFILE", "")
    monkeypatch.setenv("TEXT_TO_SQL_TEST_PROFILE", "")

    assert (
        resolve_active_profile_name(
            "", env_var="TEXT_TO_SQL_TEST_PROFILE", default="default"
        )
        == "default"
    )
