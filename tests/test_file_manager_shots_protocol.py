"""Раздел 10.2 ТЗ (docs/tz-blockout-reference-pipeline.md), критерий A42:
общий протокол записи 97_shots/shots.json — StoryBookManager/core/file_manager.py.

Проверяет саму запись на реальной файловой системе (не через фейковый Tk):
- flock на sidecar {shots_path}.lock, а не на сам файл;
- перечитывание под захваченной блокировкой и слияние ПО ПОЛЯМ внутри
  элемента — изменение, сделанное конкурентным писателем между чтением и
  записью текущего писателя, не должно теряться (иначе перечитывание
  бессмысленно, раздел 10.2);
- добавление новых и удаление пропавших элементов;
- временный файл + os.replace, без временных файлов после завершения записи;
- сохранение поведения при параллельных потоках (mutual exclusion через
  fcntl.flock не корраптит файл и не теряет ничьи поля).

Образец изоляции backup_dir/STORYBOOK_PROJECTS_DIR — tests/test_media_processor_blockout_categories.py.
"""

import json
import sys
import threading
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "StoryBookManager"))

from StoryBookManager.config.settings import app_settings  # noqa: E402
from StoryBookManager.core.file_manager import (  # noqa: E402
    FileManager,
    merge_write_shots_json,
)
import config.settings as _legacy_config_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_backup_dir(tmp_path, monkeypatch):
    backups = tmp_path / "isolated_backups"
    for settings_obj in (app_settings, _legacy_config_settings.app_settings):
        monkeypatch.setattr(settings_obj, "get_backup_directory", lambda: backups)


def _make_file_manager(tmp_path, monkeypatch, project_id="proj1"):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path / "projects"))
    return FileManager(project_id)


# ---------------------------------------------------------------------------
# merge_write_shots_json — напрямую
# ---------------------------------------------------------------------------

def test_merge_write_shots_json_preserves_concurrent_writer_field(tmp_path):
    """Основная гарантия протокола: писатель, чей снимок устарел, не должен
    затирать поле, которое за это время дописал кто-то другой (раздел 10.2,
    «иначе перечитывание бесполезно: элемент из формы... сотрёт output_path,
    который пайплайн дописал минуту назад»)."""
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)

    original_item = {
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "duration_s": 5, "timing": "00:05", "output_path": "old.png",
    }
    shots_path.write_text(json.dumps({"items": [original_item]}), encoding="utf-8")

    # Писатель A прочитал файл (baseline) — держит старый output_path.
    baseline_items = [dict(original_item)]

    # Пока A редактировал форму, конкурентный писатель (например,
    # video_generator) обновил output_path прямо на диске.
    on_disk = json.loads(shots_path.read_text(encoding="utf-8"))
    on_disk["items"][0]["output_path"] = "new_from_pipeline.png"
    shots_path.write_text(json.dumps(on_disk), encoding="utf-8")

    # A сохраняет форму: в его "свежих" данных всё то же самое, кроме
    # duration_s, которое человек поправил вручную.
    fresh_items = [dict(original_item, duration_s=10, timing="00:10")]

    merge_write_shots_json(shots_path, fresh_items, baseline_items)

    result = json.loads(shots_path.read_text(encoding="utf-8"))
    saved_item = result["items"][0]
    assert saved_item["duration_s"] == 10  # правка A сохранена
    assert saved_item["output_path"] == "new_from_pipeline.png"  # чужая правка не потеряна


def test_merge_write_shots_json_adds_new_and_removes_deleted_items(tmp_path):
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    item_keep = {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 5}
    item_delete = {"scene_number": 1, "shot_number": 2, "shot_type": "start", "duration_s": 3}
    shots_path.write_text(json.dumps({"items": [item_keep, item_delete]}), encoding="utf-8")

    baseline_items = [dict(item_keep), dict(item_delete)]
    item_new = {"scene_number": 2, "shot_number": 1, "shot_type": "start", "duration_s": 7}
    fresh_items = [dict(item_keep), item_new]  # item_delete убран из fresh

    merge_write_shots_json(shots_path, fresh_items, baseline_items)

    result = json.loads(shots_path.read_text(encoding="utf-8"))
    keys = {(it["scene_number"], it["shot_number"]) for it in result["items"]}
    assert keys == {(1, 1), (2, 1)}


def test_merge_write_shots_json_creates_sidecar_lock_not_locking_target_file(tmp_path):
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(json.dumps({"items": []}), encoding="utf-8")

    merge_write_shots_json(shots_path, [], None)

    lock_path = Path(f"{shots_path}.lock")
    assert lock_path.exists()


def test_merge_write_shots_json_leaves_no_temp_files(tmp_path):
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(json.dumps({"items": []}), encoding="utf-8")

    merge_write_shots_json(
        shots_path,
        [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 5}],
        [],
    )

    leftovers = list(shots_path.parent.glob("shots.json.*.tmp"))
    assert leftovers == []
    assert shots_path.exists()


def test_merge_write_shots_json_removes_tmp_on_replace_failure(tmp_path, monkeypatch):
    """A38 (раздел 10.2 ТЗ): если os.replace() падает между созданием
    tmp-файла и подменой shots.json (диск кончился, права, EXDEV), осиротевший
    `shots.json.<pid>.tmp` не должен оставаться на диске навсегда — тот же
    приём, что и у остальных пяти писателей общего протокола (см.
    tests/test_shots_json_writers_tmp_cleanup.py)."""
    import StoryBookManager.core.file_manager as file_manager_module

    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(file_manager_module.os, "replace", _boom)
    with pytest.raises(OSError):
        merge_write_shots_json(shots_path, [], None)

    assert list(shots_path.parent.glob("shots.json.*.tmp")) == []


def test_merge_write_shots_json_missing_baseline_treats_fresh_as_authoritative(tmp_path):
    """baseline_items=None (save_json_file вызван без предшествующего
    load_json_file этим же FileManager, раздел 10.2) — сравнивать не с чем,
    fresh целиком побеждает."""
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(
        json.dumps({"items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1}]}),
        encoding="utf-8",
    )

    fresh_items = [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 99}]
    merge_write_shots_json(shots_path, fresh_items, None)

    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert result["items"][0]["duration_s"] == 99


def test_merge_write_shots_json_concurrent_threads_do_not_lose_fields(tmp_path):
    """Несколько потоков пишут РАЗНЫЕ шоты одновременно через flock —
    ни один результат не должен потеряться, файл не должен повредиться."""
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(json.dumps({"items": []}), encoding="utf-8")

    n_writers = 8
    errors = []

    def _writer(shot_number):
        try:
            item = {
                "scene_number": 1, "shot_number": shot_number, "shot_type": "start",
                "duration_s": shot_number,
            }
            merge_write_shots_json(shots_path, [item], [])
        except Exception as exc:  # pragma: no cover - для диагностики падения потока
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    result = json.loads(shots_path.read_text(encoding="utf-8"))
    shot_numbers = {it["shot_number"] for it in result["items"]}
    assert shot_numbers == set(range(n_writers))
    assert list(shots_path.parent.glob("shots.json.*.tmp")) == []


def test_merge_write_shots_json_uses_module_level_lock(tmp_path, monkeypatch):
    """Предупреждение 1 код-ревью Э10: merge_write_shots_json() обязан
    держать тот же внутрипроцессный threading.Lock поверх fcntl.flock, что и
    эталон custom_tools/storybook/screenplay_shots_generator.py::
    _merge_write_shots() (_SHOTS_WRITE_LOCK), а не только межпроцессный flock
    (раздел 10.2). Проверяет не побочный эффект, а именно то, что функция
    реально входит/выходит из module-level лока при записи."""
    import StoryBookManager.core.file_manager as file_manager_module

    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(json.dumps({"items": []}), encoding="utf-8")

    real_lock = file_manager_module._SHOTS_WRITE_LOCK
    calls = []

    class _TrackingLock:
        def __enter__(self):
            calls.append("enter")
            return real_lock.__enter__()

        def __exit__(self, *exc_info):
            calls.append("exit")
            return real_lock.__exit__(*exc_info)

    monkeypatch.setattr(file_manager_module, "_SHOTS_WRITE_LOCK", _TrackingLock())

    merge_write_shots_json(shots_path, [], None)

    assert calls == ["enter", "exit"]


# ---------------------------------------------------------------------------
# merge_write_shots_json — слияние корневых полей документа (код-ревью Э10,
# ОШИБКА: правки корневых полей из вкладки «Редактор» → Raw JSON молча
# терялись, потому что merge_write_shots_json() сливал только "items").
# ---------------------------------------------------------------------------

def test_merge_write_shots_json_saves_root_field_edit(tmp_path):
    """Основной сценарий ревьюера: пользователь правит корневые поля
    (seed, consistency_rules) — они обязаны попасть на диск."""
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(
        json.dumps({"items": [], "seed": 111, "consistency_rules": ["rule_old"]}),
        encoding="utf-8",
    )

    baseline_root = {"items": [], "seed": 111, "consistency_rules": ["rule_old"]}
    fresh_root = {"items": [], "seed": 999, "consistency_rules": ["rule_NEW_from_user"]}

    merge_write_shots_json(shots_path, [], [], fresh_root=fresh_root, baseline_root=baseline_root)

    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert result["seed"] == 999
    assert result["consistency_rules"] == ["rule_NEW_from_user"]


def test_merge_write_shots_json_removes_deleted_root_key(tmp_path):
    """Пользователь удалил корневой ключ в Raw JSON — он обязан пропасть с диска."""
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(
        json.dumps({"items": [], "seed": 111, "extra_flag": True}),
        encoding="utf-8",
    )

    baseline_root = {"items": [], "seed": 111, "extra_flag": True}
    fresh_root = {"items": [], "seed": 111}  # extra_flag удалён пользователем

    merge_write_shots_json(shots_path, [], [], fresh_root=fresh_root, baseline_root=baseline_root)

    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert "extra_flag" not in result
    assert result["seed"] == 111


def test_merge_write_shots_json_adds_new_root_key(tmp_path):
    """Пользователь добавил новый корневой ключ — он обязан появиться на диске."""
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(json.dumps({"items": []}), encoding="utf-8")

    baseline_root = {"items": []}
    fresh_root = {"items": [], "new_field": "added_by_user"}

    merge_write_shots_json(shots_path, [], [], fresh_root=fresh_root, baseline_root=baseline_root)

    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert result["new_field"] == "added_by_user"


def test_merge_write_shots_json_preserves_concurrent_root_key(tmp_path):
    """Конкурентный писатель (например, blockout_renderer) дописал свой
    корневой ключ, пока форма редактора была открыта, — правка формы другого
    поля не должна его затереть."""
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(json.dumps({"items": [], "seed": 111}), encoding="utf-8")

    baseline_root = {"items": [], "seed": 111}
    fresh_root = {"items": [], "seed": 999}  # пользователь поменял только seed

    on_disk = json.loads(shots_path.read_text(encoding="utf-8"))
    on_disk["blockout_rendered_at"] = "2026-08-16T10:00:00Z"
    shots_path.write_text(json.dumps(on_disk), encoding="utf-8")

    merge_write_shots_json(shots_path, [], [], fresh_root=fresh_root, baseline_root=baseline_root)

    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert result["seed"] == 999
    assert result["blockout_rendered_at"] == "2026-08-16T10:00:00Z"


def test_merge_write_shots_json_fresh_root_none_leaves_root_untouched(tmp_path):
    """Обратная совместимость: вызывающий код, которому нужны только items
    (fresh_root не передан), не должен затрагивать корневые поля документа."""
    shots_path = tmp_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(json.dumps({"items": [], "seed": 111}), encoding="utf-8")

    merge_write_shots_json(shots_path, [], [])

    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert result["seed"] == 111


# ---------------------------------------------------------------------------
# FileManager.save_json_file("shots", ...) — сквозной путь со снимком
# ---------------------------------------------------------------------------

def test_save_json_file_shots_merges_against_load_snapshot(tmp_path, monkeypatch):
    fm = _make_file_manager(tmp_path, monkeypatch)
    shots_path = fm.project_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    original_item = {
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "duration_s": 5, "output_path": "old.png",
    }
    shots_path.write_text(json.dumps({"items": [original_item]}), encoding="utf-8")

    loaded = fm.load_json_file("shots")
    assert loaded["items"][0]["output_path"] == "old.png"

    # Конкурентный писатель меняет диск после load_json_file, до save_json_file.
    on_disk = json.loads(shots_path.read_text(encoding="utf-8"))
    on_disk["items"][0]["output_path"] = "new_from_pipeline.png"
    shots_path.write_text(json.dumps(on_disk), encoding="utf-8")

    loaded["items"][0]["duration_s"] = 20  # правка формы
    ok = fm.save_json_file(loaded, "shots", create_backup=False)

    assert ok is True
    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert result["items"][0]["duration_s"] == 20
    assert result["items"][0]["output_path"] == "new_from_pipeline.png"


def test_save_json_file_shots_saves_root_field_edits_from_raw_json_editor(tmp_path, monkeypatch):
    """ОШИБКА код-ревью Э10: воспроизводит сценарий ревьюера — правка seed
    и consistency_rules из вкладки «Редактор» → Raw JSON обязана сохраниться
    на диск, а не потеряться молча."""
    fm = _make_file_manager(tmp_path, monkeypatch)
    shots_path = fm.project_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    original_item = {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 5}
    shots_path.write_text(
        json.dumps({"items": [original_item], "seed": 111, "consistency_rules": ["rule_old"]}),
        encoding="utf-8",
    )

    loaded = fm.load_json_file("shots")
    assert loaded["seed"] == 111

    # Пользователь правит Raw JSON целиком, включая корневые поля.
    loaded["seed"] = 999
    loaded["consistency_rules"] = ["rule_NEW_from_user"]

    ok = fm.save_json_file(loaded, "shots", create_backup=False)

    assert ok is True
    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert result["seed"] == 999
    assert result["consistency_rules"] == ["rule_NEW_from_user"]


def test_save_json_file_shots_removes_deleted_root_key(tmp_path, monkeypatch):
    """Пользователь удалил корневой ключ в Raw JSON — он обязан пропасть с диска."""
    fm = _make_file_manager(tmp_path, monkeypatch)
    shots_path = fm.project_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(
        json.dumps({"items": [], "seed": 111, "legacy_flag": True}),
        encoding="utf-8",
    )

    loaded = fm.load_json_file("shots")
    del loaded["legacy_flag"]

    ok = fm.save_json_file(loaded, "shots", create_backup=False)

    assert ok is True
    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert "legacy_flag" not in result
    assert result["seed"] == 111


def test_save_json_file_shots_root_edit_preserves_concurrent_root_addition(tmp_path, monkeypatch):
    """Между load_json_file и save_json_file другой писатель (например,
    blockout_renderer) добавил свой корневой ключ — правка формы другого
    поля не должна его затереть (раздел 10.2)."""
    fm = _make_file_manager(tmp_path, monkeypatch)
    shots_path = fm.project_path / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    shots_path.write_text(json.dumps({"items": [], "seed": 111}), encoding="utf-8")

    loaded = fm.load_json_file("shots")

    on_disk = json.loads(shots_path.read_text(encoding="utf-8"))
    on_disk["blockout_rendered_at"] = "2026-08-16T10:00:00Z"
    shots_path.write_text(json.dumps(on_disk), encoding="utf-8")

    loaded["seed"] = 999  # правка формы
    ok = fm.save_json_file(loaded, "shots", create_backup=False)

    assert ok is True
    result = json.loads(shots_path.read_text(encoding="utf-8"))
    assert result["seed"] == 999
    assert result["blockout_rendered_at"] == "2026-08-16T10:00:00Z"
