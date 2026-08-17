"""WS-D / M-7: атомарная кросс-процессная запись shots.json в
video_generator_common.update_shots_with_descriptions (flock + tmp + os.replace).

Проверяем: (1) верхнеуровневые метаданные фильма не затираются при обновлении items,
(2) не остаётся .tmp-хвостов, (3) параллельные писатели не портят JSON.
Тесты in-process; всё пишется в tmp_path.
"""

import json
import sys
import threading
import types
from pathlib import Path


agent_command_stub = types.ModuleType("agent_command")
agent_command_stub.model_hard = "m"
agent_command_stub.model_code = "m"
agent_command_stub.model_ultimate = "m"
agent_command_stub.model_lite = "m"
sys.modules.setdefault("agent_command", agent_command_stub)

utils_stub = types.ModuleType("utils")
utils_stub.call_openai_api = lambda *a, **k: "{}"
utils_stub.extract_json_from_markdown = lambda t: t
utils_stub.parse_llm_json = lambda t: json.loads(t)
utils_stub.translate_prompts_in_items = lambda *a, **k: a[0]
sys.modules.setdefault("utils", utils_stub)

import custom_tools.storybook.video_generator_common as vgc


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _start_item(img: Path, scene=1, shot=1):
    return {
        "shot_type": "start",
        "scene_number": scene,
        "shot_number": shot,
        "output_path": str(img),
        "video_prompt": "orig vp",
    }


def test_update_preserves_toplevel_and_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(vgc, "generate_image_description", lambda *a, **k: "desc-new")
    monkeypatch.setattr(vgc, "enhance_video_prompt", lambda *a, **k: "enhanced-vp")

    img = tmp_path / "s1.png"
    img.write_bytes(b"x")
    shots_path = tmp_path / "shots.json"
    _write_json(
        shots_path,
        {
            "items": [],
            "generation_completed": True,
            "completed_scenes": [1, 2],
            "seed": 777,
            "inputs_hash": "abc",
        },
    )

    updated = vgc.update_shots_with_descriptions(str(shots_path), [_start_item(img)], force_update=True)
    assert updated > 0

    saved = json.loads(shots_path.read_text(encoding="utf-8"))
    # верхнеуровневые метаданные сохранены
    assert saved["generation_completed"] is True
    assert saved["completed_scenes"] == [1, 2]
    assert saved["seed"] == 777
    assert saved["inputs_hash"] == "abc"
    # items обновлены
    assert saved["items"][0]["scene_number"] == 1
    assert saved["items"][0]["video_prompt"] == "enhanced-vp"
    # tmp-хвост не остался
    assert list(tmp_path.glob("shots.json.*.tmp")) == []


def test_update_merges_fields_preserves_concurrent_writer_and_duration(tmp_path, monkeypatch):
    """Раздел 6.2/10.2 ТЗ: до фикса эта функция подменяла весь ключ `items`
    (`existing_data["items"] = items_list`), где `items_list` был прочитан
    вызывающим кодом ДО двух долгих фаз (описание изображений + enhance
    video_prompt, обе в пулах потоков). Если за это время другой писатель
    (blockout_renderer, ручная правка длительности) честно перечитал и
    дописал диск, его правки молча стирались. После фикса — поэлементное
    слияние: с диска берётся элемент, поверх накладываются только поля,
    которые пишет сама эта функция."""
    monkeypatch.setattr(vgc, "generate_image_description", lambda *a, **k: "desc-new")
    monkeypatch.setattr(vgc, "enhance_video_prompt", lambda *a, **k: "enhanced-vp")

    img = tmp_path / "s1.png"
    img.write_bytes(b"x")
    shots_path = tmp_path / "shots.json"

    original_item = _start_item(img)
    original_item.update({
        "duration_s": 5,
        "duration_source": "manual",
        "duration_requested_s": 5,
        "blockout_rendered_at": "2026-08-16T10:00:00Z",
        "blockout_video": "old.mp4",
    })
    # items_list, как его прочитал вызывающий код (video_generator_aitunnel_tool)
    # в начале прогона — до долгих фаз этой функции.
    items_list = [dict(original_item)]

    _write_json(shots_path, {"items": [dict(original_item)]})

    # Пока идут фазы 1/2 (в реальности — в пуле потоков), другой писатель
    # честно перечитывает и дописывает свежие данные на диск.
    on_disk = json.loads(shots_path.read_text(encoding="utf-8"))
    on_disk["items"][0]["blockout_rendered_at"] = "2026-08-16T11:00:00Z"
    on_disk["items"][0]["blockout_video"] = "new.mp4"
    on_disk["items"][0]["duration_s"] = 7
    _write_json(shots_path, on_disk)

    updated = vgc.update_shots_with_descriptions(str(shots_path), items_list, force_update=True)
    assert updated > 0

    saved = json.loads(shots_path.read_text(encoding="utf-8"))
    saved_item = saved["items"][0]
    # video_prompt действительно улучшен этим шагом.
    assert saved_item["video_prompt"] == "enhanced-vp"
    # ...но конкурентная запись не откатывается.
    assert saved_item["blockout_rendered_at"] == "2026-08-16T11:00:00Z"
    assert saved_item["blockout_video"] == "new.mp4"
    # ...и длительность не теряется/не откатывается к устаревшему снимку.
    assert saved_item["duration_s"] == 7
    assert saved_item["duration_source"] == "manual"
    assert saved_item["duration_requested_s"] == 5


def test_update_shots_removes_tmp_on_replace_failure(tmp_path, monkeypatch):
    """A38: осиротевший временный файл не должен оставаться, если запись
    оборвалась исключением между созданием tmp-файла и os.replace. Функция не
    поднимает исключение наверх (см. общий try/except вокруг записи) — просто
    возвращает -1 и логирует ошибку. -1 (а не 0) отличает "запись не
    удалась" от "обновлять было нечего": вызывающие (video_generator*.py)
    проверяют `descriptions_updated < 0`, чтобы не перечитывать с диска
    устаревшую версию поверх уже обновлённых в памяти items_list."""
    monkeypatch.setattr(vgc, "generate_image_description", lambda *a, **k: "desc-new")
    monkeypatch.setattr(vgc, "enhance_video_prompt", lambda *a, **k: "enhanced-vp")

    img = tmp_path / "s1.png"
    img.write_bytes(b"x")
    shots_path = tmp_path / "shots.json"
    _write_json(shots_path, {"items": []})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(vgc.os, "replace", boom)

    result = vgc.update_shots_with_descriptions(str(shots_path), [_start_item(img)], force_update=True)
    assert result == -1
    assert list(tmp_path.glob("shots.json.*.tmp")) == []


def test_concurrent_writers_keep_json_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(vgc, "generate_image_description", lambda *a, **k: "d")
    monkeypatch.setattr(vgc, "enhance_video_prompt", lambda *a, **k: "e")

    shots_path = tmp_path / "shots.json"
    _write_json(shots_path, {"items": [], "seed": 1})

    imgs = []
    for i in range(6):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"x")
        imgs.append(p)

    def worker(idx):
        vgc.update_shots_with_descriptions(
            str(shots_path), [_start_item(imgs[idx], scene=idx + 1)], force_update=True
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # файл остаётся валидным JSON (flock защитил от повреждения)
    saved = json.loads(shots_path.read_text(encoding="utf-8"))
    assert isinstance(saved, dict)
    assert isinstance(saved.get("items"), list)
    assert list(tmp_path.glob("shots.json.*.tmp")) == []
