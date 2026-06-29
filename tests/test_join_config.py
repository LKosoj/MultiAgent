"""
Тесты для join_config (конфиг join-параметров text-to-sql).

Покрывают:
  * загрузку default-профиля из репо-yaml (greedy/off/0.1/8/1000);
  * агрессивный muni_ru (graph/validate);
  * парсинг `"off"` как строки, а не bool;
  * env-оверрайд каждого ключа (env > yaml), пустой env → yaml;
  * профиль-env + value-env (value побеждает);
  * fail-fast: отсутствие файла, не-mapping, нет default, пропуск ключа,
    неизвестный профиль, неизвестный enum (yaml/env), невалидный float/int env,
    значение вне диапазона, max_terminals=0, max_terminals=true(bool).
"""

from pathlib import Path

import pytest

from custom_tools.text_to_sql import join_config as jc


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Каждый тест начинается с пустого кэша и без env-переменных."""
    for var in (
        "TEXT_TO_SQL_JOINS_PATH",
        "TEXT_TO_SQL_JOINS_PROFILE",
        "TEXT_TO_SQL_JOIN_PATH_ALGO",
        "TEXT_TO_SQL_JOIN_CONTAINMENT",
        "TEXT_TO_SQL_JOIN_MIN_CONTAINMENT",
        "TEXT_TO_SQL_JOIN_PATH_MAX_TERMINALS",
        "TEXT_TO_SQL_CONTAINMENT_SAMPLE",
    ):
        monkeypatch.delenv(var, raising=False)
    jc.reset_cache()
    yield
    jc.reset_cache()


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "joins.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- загрузка из репо-yaml --------------------------------------------------


def test_loads_default_profile_from_repo_yaml():
    """Конфиг из репозитория грузится; default консервативен."""
    profile = jc.load_join_config()
    assert profile.name == "default"
    assert profile.path_algo == "greedy"
    assert profile.containment == "off"
    assert profile.min_containment == pytest.approx(0.1)
    assert profile.max_terminals == 8
    assert profile.containment_sample == 1000


def test_muni_ru_is_aggressive(monkeypatch):
    """Профиль muni_ru включает граф и containment validate."""
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PROFILE", "muni_ru")
    jc.reset_cache()
    assert jc.resolve_active_profile() == "muni_ru"
    profile = jc.load_join_config()
    assert profile.name == "muni_ru"
    assert profile.path_algo == "graph"
    assert profile.containment == "validate"


def test_off_parsed_as_string_not_bool():
    """`"off"` в yaml — строка, а не bool False (YAML 1.1 ловушка)."""
    profile = jc.load_join_config()
    assert profile.containment == "off"
    assert isinstance(profile.containment, str)


# --- env-оверрайд каждого ключа --------------------------------------------


def test_env_overrides_path_algo(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "graph")
    assert jc.resolve_path_algo() == "graph"


def test_env_overrides_containment(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_CONTAINMENT", "weight")
    assert jc.resolve_containment() == "weight"


def test_env_overrides_min_containment(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_MIN_CONTAINMENT", "0.42")
    assert jc.resolve_min_containment() == pytest.approx(0.42)


def test_env_overrides_max_terminals(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_MAX_TERMINALS", "12")
    assert jc.resolve_max_terminals() == 12


def test_env_overrides_containment_sample(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_CONTAINMENT_SAMPLE", "500")
    assert jc.resolve_containment_sample() == 500


# --- пустой env → yaml ------------------------------------------------------


def test_empty_env_falls_back_to_yaml(monkeypatch):
    """Пустая env-переменная не считается «задана» — берётся yaml."""
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "")
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_MIN_CONTAINMENT", "")
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_MAX_TERMINALS", "")
    assert jc.resolve_path_algo() == "greedy"
    assert jc.resolve_min_containment() == pytest.approx(0.1)
    assert jc.resolve_max_terminals() == 8


# --- профиль-env + value-env (value побеждает) ------------------------------


def test_value_env_wins_over_profile_env(monkeypatch):
    """Профиль muni_ru даёт graph, но per-key env переопределяет на greedy."""
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PROFILE", "muni_ru")
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "greedy")
    jc.reset_cache()
    # профиль активный — muni_ru (graph), но value-env побеждает
    assert jc.load_join_config().path_algo == "graph"
    assert jc.resolve_path_algo() == "greedy"


# --- fail-fast: файл/структура ---------------------------------------------


def test_missing_file_raises(tmp_path, monkeypatch):
    """Отсутствие файла → FileNotFoundError (no silent fallback)."""
    monkeypatch.setenv(
        "TEXT_TO_SQL_JOINS_PATH", str(tmp_path / "no-such-file.yaml")
    )
    jc.reset_cache()
    with pytest.raises(FileNotFoundError):
        jc.load_join_config()


def test_top_level_must_be_mapping(tmp_path, monkeypatch):
    yaml_path = _write_yaml(tmp_path, "- a\n- b\n")
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PATH", str(yaml_path))
    jc.reset_cache()
    with pytest.raises(ValueError, match="mapping"):
        jc.load_join_config()


def test_profiles_must_contain_default(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  muni_ru:
    path_algo: graph
    containment: validate
    min_containment: 0.1
    max_terminals: 8
    containment_sample: 1000
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PATH", str(yaml_path))
    jc.reset_cache()
    with pytest.raises(ValueError, match="default"):
        jc.load_join_config()


def test_missing_required_field(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  default:
    path_algo: greedy
    containment: "off"
    min_containment: 0.1
    max_terminals: 8
    # containment_sample пропущено
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PATH", str(yaml_path))
    jc.reset_cache()
    with pytest.raises(ValueError, match="containment_sample"):
        jc.load_join_config()


def test_unknown_profile_raises_keyerror(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  default:
    path_algo: greedy
    containment: "off"
    min_containment: 0.1
    max_terminals: 8
    containment_sample: 1000
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PATH", str(yaml_path))
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PROFILE", "no_such_profile")
    jc.reset_cache()
    with pytest.raises(KeyError, match="unknown profile"):
        jc.load_join_config()


# --- fail-fast: значения ----------------------------------------------------


def test_unknown_enum_in_yaml(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  default:
    path_algo: bogus
    containment: "off"
    min_containment: 0.1
    max_terminals: 8
    containment_sample: 1000
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PATH", str(yaml_path))
    jc.reset_cache()
    with pytest.raises(ValueError, match="path_algo"):
        jc.load_join_config()


def test_unknown_enum_in_env(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_CONTAINMENT", "bogus")
    with pytest.raises(ValueError, match="TEXT_TO_SQL_JOIN_CONTAINMENT"):
        jc.resolve_containment()


def test_invalid_float_env(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_MIN_CONTAINMENT", "not-a-number")
    with pytest.raises(ValueError, match="TEXT_TO_SQL_JOIN_MIN_CONTAINMENT"):
        jc.resolve_min_containment()


def test_invalid_int_env(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_MAX_TERMINALS", "not-an-int")
    with pytest.raises(ValueError, match="TEXT_TO_SQL_JOIN_PATH_MAX_TERMINALS"):
        jc.resolve_max_terminals()


def test_min_containment_out_of_range_yaml(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  default:
    path_algo: greedy
    containment: "off"
    min_containment: 1.5
    max_terminals: 8
    containment_sample: 1000
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PATH", str(yaml_path))
    jc.reset_cache()
    with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
        jc.load_join_config()


def test_min_containment_out_of_range_env(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_MIN_CONTAINMENT", "1.5")
    with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
        jc.resolve_min_containment()


def test_max_terminals_zero_yaml(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  default:
    path_algo: greedy
    containment: "off"
    min_containment: 0.1
    max_terminals: 0
    containment_sample: 1000
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PATH", str(yaml_path))
    jc.reset_cache()
    with pytest.raises(ValueError, match="max_terminals"):
        jc.load_join_config()


def test_max_terminals_bool_rejected(tmp_path, monkeypatch):
    """max_terminals: true (bool) → ValueError (bool не int)."""
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  default:
    path_algo: greedy
    containment: "off"
    min_containment: 0.1
    max_terminals: true
    containment_sample: 1000
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_JOINS_PATH", str(yaml_path))
    jc.reset_cache()
    with pytest.raises(ValueError, match="max_terminals"):
        jc.load_join_config()
