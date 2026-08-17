"""Э0.3: нормализация длительности шотов для болванки (blockout).

Покрывает мандатный минимум тестов из ТЗ (docs/tz-blockout-reference-pipeline.md,
раздел 6.2): идемпотентность, неизменяемость duration_requested_s, выживание
ручной длительности при полной перезаписи (A24), P20 на битой ручной метке,
непарный шот, пустое пересечение allowed_durations (блок 4), дописывание
warnings во вторую запись video_model_caps.json (блок 2), правило нулевой суммы
+ P19, арифметика перехода в photo FCPXML, сведение _calculate_total_duration
(было 108 элементов × 5 + 107 «на переход» = 647с, стало 270с), выживание
duration_s/duration_source/duration_requested_s при merge в shots_prompt_qa.
"""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import custom_tools.storybook.screenplay_shots_generator as gen
import custom_tools.storybook.shots_prompt_qa as sq
from custom_tools.storybook.screenplay_shots_generator_utils import fcpxml_generator as fg
from custom_tools.storybook.screenplay_shots_generator_utils.timing_utils import (
    _calculate_total_duration,
    _calculate_shot_durations_from_timestamps,
)
from custom_tools.storybook.video_generator_common import (
    resolve_effective_durations,
    append_video_model_caps_warnings,
    _read_previous_video_caps,
)


# === Секция A: resolve_effective_durations (блок 4 ТЗ) =========================

def test_resolve_effective_durations_no_allowed_returns_full_set():
    effective, warnings = resolve_effective_durations([5, 7, 10], None)
    assert effective == [5, 7, 10]
    assert warnings == []


def test_resolve_effective_durations_narrows_and_warns_dropped():
    effective, warnings = resolve_effective_durations([5, 7, 10], [7, 10, 99])
    assert effective == [7, 10]
    assert len(warnings) == 1
    assert warnings[0]["details"]["dropped"] == [99]


def test_resolve_effective_durations_empty_intersection_falls_back_to_full_set():
    effective, warnings = resolve_effective_durations([5, 7, 10], [1, 2, 3])
    # Пересечение пусто -> ограничение проекта игнорируется целиком, берём полный набор.
    assert effective == [5, 7, 10]
    assert len(warnings) == 1
    assert warnings[0]["level"] == "error"
    assert warnings[0]["code"] == "DURATION_ALLOWED_EMPTY_INTERSECTION"


# === Секция B: append_video_model_caps_warnings ("запись 2", блок 2 ТЗ) =========

def test_append_video_model_caps_warnings_appends_to_existing_list(tmp_path):
    caps_path = tmp_path / "video_model_caps.json"
    caps_path.write_text(
        json.dumps({"tool_name": "x", "supported_durations": [5], "warnings": [{"code": "P14", "level": "warning", "message": "m1", "details": {}}]}),
        encoding="utf-8",
    )
    new_warnings = [{"code": "P08", "level": "warning", "message": "m2", "details": {}}]
    result = append_video_model_caps_warnings(str(caps_path), new_warnings)
    assert [w["code"] for w in result["warnings"]] == ["P14", "P08"]
    on_disk = json.loads(caps_path.read_text(encoding="utf-8"))
    assert [w["code"] for w in on_disk["warnings"]] == ["P14", "P08"]
    # Остальные поля файла не тронуты.
    assert on_disk["supported_durations"] == [5]


def test_append_video_model_caps_warnings_noop_when_file_missing(tmp_path):
    caps_path = tmp_path / "video_model_caps.json"
    result = append_video_model_caps_warnings(str(caps_path), [{"code": "P08", "level": "warning", "message": "m", "details": {}}])
    assert result == {}
    assert not caps_path.exists()


def test_append_video_model_caps_warnings_noop_when_no_new_warnings(tmp_path):
    caps_path = tmp_path / "video_model_caps.json"
    caps_path.write_text(json.dumps({"warnings": []}), encoding="utf-8")
    result = append_video_model_caps_warnings(str(caps_path), [])
    assert result == {}
    on_disk = json.loads(caps_path.read_text(encoding="utf-8"))
    assert on_disk == {"warnings": []}


# === Секция C: _normalize_shot_durations (блок 5 ТЗ) ============================

def _pair(scene, shot, timing="00:05", extra_start=None, extra_end=None):
    start = {"scene_number": scene, "shot_number": shot, "shot_type": "start", "timing": timing}
    end = {"scene_number": scene, "shot_number": shot, "shot_type": "end", "timing": timing}
    if extra_start:
        start.update(extra_start)
    if extra_end:
        end.update(extra_end)
    return [start, end]


def test_normalize_is_idempotent():
    items = _pair(1, 1, timing="00:07")
    effective = [5, 7, 10]
    changed1, _, _ = gen._normalize_shot_durations(items, effective)
    snapshot = json.loads(json.dumps(items))
    changed2, warnings2, affected2 = gen._normalize_shot_durations(items, effective)
    assert changed1 is True
    assert changed2 is False
    assert warnings2 == []
    assert affected2 == []
    assert items == snapshot


def test_duration_requested_s_never_overwritten_across_passes():
    items = _pair(1, 1, timing="00:03")
    effective = [5, 7, 10]
    gen._normalize_shot_durations(items, effective)
    assert items[0]["duration_requested_s"] == 3
    # Второй прогон с другим набором не должен переписать duration_requested_s,
    # только пересчитать duration_s заново от того же requested.
    gen._normalize_shot_durations(items, [1, 2, 3])
    assert items[0]["duration_requested_s"] == 3
    assert items[0]["duration_s"] == 3  # 3 уже входит в новый набор -> "timing"
    assert items[0]["duration_source"] == "timing"


def test_automatic_branch_duration_source_timing_vs_model_catalog():
    # requested (5, из timing) уже в наборе -> "timing", замены не было.
    exact = _pair(1, 1, timing="00:05")
    gen._normalize_shot_durations(exact, [5, 7, 10])
    assert exact[0]["duration_source"] == "timing"
    assert exact[0]["duration_s"] == 5

    # requested (6) не в наборе -> snap к ближайшему (5) -> "model_catalog".
    snapped = _pair(2, 1, timing="00:06")
    gen._normalize_shot_durations(snapped, [5, 7, 10])
    assert snapped[0]["duration_source"] == "model_catalog"
    assert snapped[0]["duration_s"] == 5


def test_manual_value_survives_and_requested_s_from_old_timing_when_absent():
    """A24-подобная проверка на уровне самой функции нормализации: ручная метка
    без duration_requested_s заполняет его разбором СТАРОГО timing (до перезаписи),
    а не значением duration_s."""
    items = _pair(
        1, 1, timing="00:07",
        extra_start={"duration_source": "manual", "duration_s": 9},
        extra_end={"duration_source": "manual", "duration_s": 9},
    )
    changed, warnings, affected = gen._normalize_shot_durations(items, [5, 9, 10])
    # duration_s уже был 9 (ручное значение проставлено заранее) -> не "изменился"
    # в этом проходе, изменился лишь ранее отсутствовавший duration_requested_s.
    assert changed is False
    assert warnings == []
    for el in items:
        assert el["duration_s"] == 9
        assert el["duration_source"] == "manual"
        # requested_s — из СТАРОГО timing "00:07" (=7), а не из manual value (9).
        assert el["duration_requested_s"] == 7
        assert el["timing"] == "00:09"


def test_manual_value_not_in_effective_durations_raises_b01():
    items = _pair(1, 1, extra_start={"duration_source": "manual", "duration_s": 6}, extra_end={"duration_source": "manual", "duration_s": 6})
    with pytest.raises(gen._ManualDurationNotSupportedError):
        gen._normalize_shot_durations(items, [5, 7, 10])


def test_manual_mark_without_int_duration_s_ignored_with_p20():
    items = _pair(1, 1, timing="00:06", extra_start={"duration_source": "manual", "duration_s": "not-an-int"})
    changed, warnings, affected = gen._normalize_shot_durations(items, [5, 7, 10])
    assert any(w["code"] == "P20" for w in warnings)
    # Метка проигнорирована -> обычная автоматическая нормализация (snap 6 -> 5).
    assert items[0]["duration_s"] == 5
    assert items[0]["duration_source"] == "model_catalog"


def test_manual_pair_one_valid_one_broken_mark_no_p20():
    """Код-ревью Э0.3: если у ОДНОГО элемента пары ручная метка валидна, а у
    другого — битая (duration_s не целое/отсутствует), ручное значение всё
    равно применяется (start побеждает), и P20 НЕ должен выдаваться — иначе
    это ложный сигнал, что ручная длительность проигнорирована, хотя она
    реально отработала."""
    items = _pair(
        1, 1, timing="00:06",
        extra_start={"duration_source": "manual", "duration_s": 5},
        extra_end={"duration_source": "manual", "duration_s": None},
    )
    changed, warnings, affected = gen._normalize_shot_durations(items, [5, 7, 10])
    assert warnings == []
    for el in items:
        assert el["duration_s"] == 5
        assert el["duration_source"] == "manual"


def test_manual_pair_both_broken_marks_emits_p20_and_automatic_branch():
    """Настоящий случай P20: у ОБОИХ элементов пары ручная метка битая ->
    ни одной валидной ручной длительности не осталось -> шот идёт обычной
    автоматической веткой, и предупреждение P20 выдаётся."""
    items = _pair(
        1, 1, timing="00:06",
        extra_start={"duration_source": "manual", "duration_s": None},
        extra_end={"duration_source": "manual"},  # duration_s вовсе отсутствует
    )
    changed, warnings, affected = gen._normalize_shot_durations(items, [5, 7, 10])
    assert [w["code"] for w in warnings] == ["P20"]
    # Метки проигнорированы -> обычная автоматическая нормализация (snap 6 -> 5).
    assert items[0]["duration_s"] == 5
    assert items[0]["duration_source"] == "model_catalog"
    assert items[1]["duration_s"] == 5
    assert items[1]["duration_source"] == "model_catalog"


def test_manual_pair_disagreement_start_wins():
    items = _pair(
        1, 1,
        extra_start={"duration_source": "manual", "duration_s": 7},
        extra_end={"duration_source": "manual", "duration_s": 10},
    )
    gen._normalize_shot_durations(items, [5, 7, 10])
    assert items[0]["duration_s"] == 7
    assert items[1]["duration_s"] == 7  # end приведён к start


def test_unpaired_shot_normalized_without_error():
    items = [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "timing": "00:06"}]
    changed, warnings, affected = gen._normalize_shot_durations(items, [5, 7, 10])
    assert changed is True
    # requested=6 равноудалён от 5 и 7 (|6-5|=1, |6-7|=1) -> при ничьей побеждает меньшее.
    assert items[0]["duration_s"] == 5


# === Секция D: перенос ручных длительностей при полной перезаписи (блок 6, A24) =

def test_manual_duration_survives_full_rewrite_via_overrides():
    on_disk_items = _pair(
        1, 1, timing="00:09",
        extra_start={"duration_source": "manual", "duration_s": 9, "duration_requested_s": 4},
        extra_end={"duration_source": "manual", "duration_s": 9, "duration_requested_s": 4},
    )
    overrides = gen._build_manual_duration_overrides(on_disk_items)
    # Лукап ключуется по (scene, shot, shot_type) -> по записи на start и на end.
    assert len(overrides) == 2

    # Свежие items из "полной перегенерации" — без единого поля длительности,
    # ровно как их собирает _create_shot_item (раздел 6.2 ТЗ, "обязательное условие").
    fresh_items = [
        {"scene_number": 1, "shot_number": 1, "shot_type": "start", "timing": "00:02"},
        {"scene_number": 1, "shot_number": 1, "shot_type": "end", "timing": "00:02"},
    ]
    gen._apply_manual_duration_overrides(fresh_items, overrides)
    changed, warnings, affected = gen._normalize_shot_durations(fresh_items, [5, 9, 10])
    for el in fresh_items:
        assert el["duration_s"] == 9
        assert el["duration_source"] == "manual"
        assert el["duration_requested_s"] == 4  # перенесено с диска, не тронуто


def test_manual_duration_overrides_skips_broken_marks():
    on_disk_items = [
        {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_source": "manual", "duration_s": "bad"},
    ]
    overrides = gen._build_manual_duration_overrides(on_disk_items)
    assert overrides == {}


# === Секция E: fcpxml_generator (блок 8 ТЗ) =====================================

def _shot_item(scene, shot, shot_type, duration_s, timing="00:05"):
    return {
        "scene_number": scene, "shot_number": shot, "shot_type": shot_type,
        "duration_s": duration_s, "timing": timing,
        "image_path": f"/tmp/{scene}_{shot}_{shot_type}.png",
    }


def test_generate_photo_fcpxml_zero_sum_rule_returns_false_and_removes_stale_file(tmp_path):
    fcpxml_path = tmp_path / "photo_shots_timeline.fcpxml"
    fcpxml_path.write_text("<stale/>", encoding="utf-8")
    # Все шоты непарные (только start) -> ни одной пары.
    items = [_shot_item(1, 1, "start", 5), _shot_item(1, 2, "start", 5)]
    result = fg._generate_photo_fcpxml("proj", items, str(fcpxml_path))
    assert result is False
    assert not fcpxml_path.exists()


def test_generate_photo_fcpxml_transition_arithmetic_duration_1(tmp_path):
    """duration_s=1 -> T=min(1.0, 0.5)=0.5; кадры по 0.25с + переход 0.5с = 1.0с ровно."""
    fcpxml_path = tmp_path / "photo_shots_timeline.fcpxml"
    items = [_shot_item(1, 1, "start", 1), _shot_item(1, 1, "end", 1)]
    result = fg._generate_photo_fcpxml("proj", items, str(fcpxml_path))
    assert result is True
    tree = ET.parse(str(fcpxml_path))
    root = tree.getroot()
    clips = root.findall(".//spine/*")
    # 2 asset-clip (start/end) + 1 transition = 3 узла на дорожке.
    assert len(clips) == 3
    durations_6000 = []
    for clip in clips:
        dur = clip.get("duration", "")
        if dur.endswith("/6000s"):
            durations_6000.append(int(dur.split("/")[0]))
    total_ticks = sum(durations_6000)
    # 1с на сетке 6000 тиков/с -> сумма длительностей клипов = 6000 (с округлением int()).
    assert abs(total_ticks - 6000) <= 2


def test_calculate_total_duration_reduces_paired_elements_not_double_counted():
    """Регрессия раздела 6.2 ТЗ: на 108 элементах (54 парных шота по 5с) старая
    формула (108*5 + 107 "на переход") давала 647с; сведённая по шотам даёт 270с
    (54 * 5), без двойного учёта пары и без добавки "на переход"."""
    items = []
    for shot_num in range(1, 55):  # 54 шота
        items.append({"scene_number": 1, "shot_number": shot_num, "shot_type": "start", "duration_s": 5})
        items.append({"scene_number": 1, "shot_number": shot_num, "shot_type": "end", "duration_s": 5})
    assert len(items) == 108

    shots_by_key = {}
    for it in items:
        key = f"scene_{it['scene_number']:02d}_shot_{it['shot_number']:02d}"
        shots_by_key.setdefault(key, {"start": None, "end": None})
        shots_by_key[key][it["shot_type"]] = it
    shot_durations_map = _calculate_shot_durations_from_timestamps(shots_by_key)

    total = _calculate_total_duration(items, shot_durations_map)
    assert total == 270.0


def test_calculate_total_duration_skips_unpaired_shots():
    items = [
        {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 5},
        {"scene_number": 1, "shot_number": 1, "shot_type": "end", "duration_s": 5},
        {"scene_number": 1, "shot_number": 2, "shot_type": "start", "duration_s": 5},  # непарный
    ]
    total = _calculate_total_duration(items, {})
    assert total == 5.0  # только парный шот 1 учтён


# === Секция F: shots_prompt_qa — выживание полей длительности при merge =========

def _screenplay_scene(n):
    return {
        "scene_number": n, "action": "", "characters": [], "location_time": "",
        "storyboard": [{"shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "", "timing": "0-1"}],
    }


def test_shots_prompt_qa_merge_preserves_concurrently_written_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    pid = "proj"
    screenplay_dir = tmp_path / pid / "91_screenplay"
    screenplay_dir.mkdir(parents=True)
    (screenplay_dir / "screenplay.json").write_text(
        json.dumps({"screenplay": [_screenplay_scene(1)]}), encoding="utf-8"
    )

    shots_dir = tmp_path / pid / "97_shots"
    shots_dir.mkdir(parents=True)
    on_disk_item = {
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "video_prompt": "old", "english_prompt": "old", "negative_prompt": "",
        "reference_image_paths": [], "_shot_frame_spec": {},
        "duration_s": 7, "duration_source": "manual", "duration_requested_s": 3,
        # поле, дописанное параллельным процессом (например, blockout_renderer)
        # МЕЖДУ чтением QA и его собственной записью:
        "blockout_render_path": "/tmp/blockout/1-1.mp4",
    }
    (shots_dir / "shots.json").write_text(
        json.dumps({"items": [on_disk_item], "consistency_rules": []}), encoding="utf-8"
    )

    monkeypatch.setattr(sq, "call_openai_api", lambda **k: '{"repairs": [], "notes": "ok"}')
    monkeypatch.setattr(sq, "_extract_shot_frame_spec_llm", lambda **k: {})

    qa_input_item = dict(on_disk_item)
    qa_input_item.pop("blockout_render_path")  # QA не знает про это поле вовсе
    qa_input_item["video_prompt"] = "new-from-qa"

    sq.shots_prompt_qa_tool(
        session_id="s", project_id=pid,
        shots_data={"items": [qa_input_item], "consistency_rules": []},
        enable=True, force=True, model="hard",
        scene_numbers=None, global_max_repairs=0, dry_run=False,
    )

    saved = json.loads((shots_dir / "shots.json").read_text(encoding="utf-8"))
    saved_item = saved["items"][0]
    # Поле, дописанное конкурентно, выжило (не заменено целиком одноимённым из памяти).
    assert saved_item["blockout_render_path"] == "/tmp/blockout/1-1.mp4"
    # Собственное обновление QA применилось.
    assert saved_item["video_prompt"] == "new-from-qa"
    # Поля длительности не потеряны.
    assert saved_item["duration_s"] == 7
    assert saved_item["duration_source"] == "manual"
    assert saved_item["duration_requested_s"] == 3


# === Секция G: дисциплина atomic-записи shots_prompt_qa (раздел 20.3/21 ТЗ) ======

def test_write_json_atomic_tmp_name_contains_pid(tmp_path, monkeypatch):
    """Уникальность временного файла (раздел 20.3 ТЗ): два конкурентных процесса
    не должны писать в один и тот же tmp-файл, иначе один затирает другой до
    os.replace. Имя должно включать os.getpid()."""
    target = tmp_path / "out.json"
    captured_src = []
    real_replace = os.replace

    def spy_replace(src, dst):
        captured_src.append(src)
        return real_replace(src, dst)

    monkeypatch.setattr(sq.os, "replace", spy_replace)
    sq._write_json_atomic(str(target), {"a": 1})

    assert captured_src == [f"{target}.{os.getpid()}.tmp"]
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


def test_shots_prompt_qa_write_locks_sidecar_not_target(tmp_path, monkeypatch):
    """Раздел 20.3/21 ТЗ: блокировка должна ставиться на sidecar {path}.lock,
    а не на сам shots_path — иначе os.replace меняет inode, и второй процесс
    флокает уже отсоединённый от каталога файл (окно потерянного апдейта)."""
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    pid = "proj"
    screenplay_dir = tmp_path / pid / "91_screenplay"
    screenplay_dir.mkdir(parents=True)
    (screenplay_dir / "screenplay.json").write_text(
        json.dumps({"screenplay": [_screenplay_scene(1)]}), encoding="utf-8"
    )
    shots_dir = tmp_path / pid / "97_shots"
    shots_dir.mkdir(parents=True)
    shots_path = shots_dir / "shots.json"
    shots_path.write_text(json.dumps({"items": [], "consistency_rules": []}), encoding="utf-8")

    monkeypatch.setattr(sq, "call_openai_api", lambda **k: '{"repairs": [], "notes": "ok"}')
    monkeypatch.setattr(sq, "_extract_shot_frame_spec_llm", lambda **k: {})

    item = {
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "video_prompt": "x", "english_prompt": "x", "negative_prompt": "",
        "reference_image_paths": [], "_shot_frame_spec": {},
    }
    sq.shots_prompt_qa_tool(
        session_id="s", project_id=pid,
        shots_data={"items": [item], "consistency_rules": []},
        enable=True, force=True, model="hard",
        scene_numbers=None, global_max_repairs=0, dry_run=False,
    )

    # Sidecar-файл блокировки создан рядом с shots.json (а не сам shots.json флокнут).
    assert (shots_dir / "shots.json.lock").exists()
    # Целевой файл записан штатно (не остался залоченным на подменённом inode).
    saved = json.loads(shots_path.read_text(encoding="utf-8"))
    assert saved["items"][0]["video_prompt"] == "x"
    # Ни один tmp-файл не остался на диске после успешного os.replace.
    assert list(shots_dir.glob("shots.json.*.tmp")) == []
