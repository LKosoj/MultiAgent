#!/usr/bin/env python3
"""Разовая миграция input_hash в 97_shots/provider_jobs.json (раздел 19.1 ТЗ).

Формула хеша менялась (правило M-6, раздел 6.1 ТЗ): в хеш входа должна попадать
только запрошенная пользователем длительность (из timing шота), а не подогнанная
под supported_durations модели — иначе смена каталога моделей массово
инвалидирует уже оплаченные задания. Этот скрипт пересчитывает input_hash
существующих записей под текущую _CURRENT_HASH_INPUTS_VERSION, используя ТУ ЖЕ
формулу хеша, что и живой код провайдеров (_build_input_hash из
video_generator_aitunnel_jobs.py — переизобретать её здесь нельзя).

Использование:
    python3 scripts/migrate_provider_jobs_hash.py --project-dir plots/storybooks/<project_id> [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from custom_tools.storybook.video_generator_aitunnel_jobs import (  # noqa: E402
    _CURRENT_HASH_INPUTS_VERSION,
    _ProviderJobStore,
    _build_input_hash,
)
from custom_tools.storybook.video_generator_common import (  # noqa: E402
    _VIDEO_MODEL_CAPABILITIES_REGISTRY,
    parse_duration_seconds_from_timing,
)


def _load_shots_by_key(shots_path: Path) -> Dict[str, Dict[str, Any]]:
    if not shots_path.exists():
        return {}
    with shots_path.open("r", encoding="utf-8") as shots_file:
        shots_data = json.load(shots_file)
    items = shots_data.get("items", []) if isinstance(shots_data, dict) else shots_data
    shots_by_key: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        shot_key = f"{item.get('scene_number', '?')}-{item.get('shot_number', '?')}"
        shots_by_key[shot_key] = item
    return shots_by_key


def _frame_types(source_image_hashes: Dict[str, Optional[str]]) -> List[str]:
    return ["first_frame"] + (["last_frame"] if source_image_hashes.get("end_image") else [])


def _recompute_input_hash(job: Dict[str, Any], shot: Dict[str, Any]) -> Optional[str]:
    """Пересчитывает input_hash по той же формуле, что и живой код провайдера.

    Значение, которое уже сохранено в самой записи job (prompt_hash,
    source_image_hashes, model — для aitunnel, а для minimax — seed), берётся
    ИЗ ЗАПИСИ, а не из окружения на момент запуска миграции: окружение могло
    измениться со времени генерации, и подмена сохранённого значения текущим
    окружением даёт хеш, который не совпадёт с тем, что вычислил боевой код.
    Из ТЕКУЩЕГО окружения/реестра каталогов берётся только то, чего в записи
    физически нет (GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_LOCATION для veo, реестр
    возможностей моделей для kling/minimax) — так же, как это делает живой код
    при следующем реальном запуске генерации.
    """
    provider = job.get("provider")
    prompt_hash = str(job.get("prompt_hash") or "")
    source_image_hashes = job.get("source_image_hashes") or {}
    frame_types = _frame_types(source_image_hashes)

    if provider == "aitunnel":
        # A31: имя модели уже сохранено в записи (job["model"]) — это то, что
        # реально пошло в боевой хеш при генерации. Брать его из
        # AITUNNEL_VIDEO_MODEL на момент миграции нельзя: если переменная в
        # оболочке миграции не задана или указывает на другую модель, хеш не
        # совпадёт с боевым, запись перестанет ловиться legacy-сопоставлением
        # (allow_legacy_identity) и уже оплаченный клип будет сгенерирован и
        # оплачен заново.
        configured_model = str(job.get("model") or "").strip()
        requested_duration = parse_duration_seconds_from_timing(shot.get("timing", "00:00 - 00:06"))
        requested_width = int(shot.get("width") or 0)
        requested_height = int(shot.get("height") or 0)
        return _build_input_hash(
            model_name=configured_model,
            prompt_hash=prompt_hash,
            source_image_hashes=source_image_hashes,
            requested_duration=requested_duration,
            requested_width=requested_width,
            requested_height=requested_height,
            seed=None,
            frame_types=frame_types,
            provider_name="aitunnel",
        )

    if provider == "minimax":
        caps = _VIDEO_MODEL_CAPABILITIES_REGISTRY["video_generator_mm_tool"]
        requested_duration = parse_duration_seconds_from_timing(shot.get("timing", "00:00 - 00:06"))
        return _build_input_hash(
            model_name=caps["model"],
            prompt_hash=prompt_hash,
            source_image_hashes=source_image_hashes,
            requested_duration=requested_duration,
            requested_width=0,
            requested_height=0,
            seed=job.get("seed"),
            frame_types=frame_types,
            provider_name="minimax",
        )

    if provider == "kling":
        caps = _VIDEO_MODEL_CAPABILITIES_REGISTRY["video_generator_tool"]
        requested_duration = parse_duration_seconds_from_timing(shot.get("timing", "00:00 - 00:06"))
        model_name = f"{caps['model']}|{caps['mode']}|{caps['aspect_ratio']}"
        return _build_input_hash(
            model_name=model_name,
            prompt_hash=prompt_hash,
            source_image_hashes=source_image_hashes,
            requested_duration=requested_duration,
            requested_width=0,
            requested_height=0,
            seed=None,
            frame_types=frame_types,
            provider_name="kling",
        )

    if provider == "veo":
        caps = _VIDEO_MODEL_CAPABILITIES_REGISTRY["video_generator_veo_tool"]
        project_id_vertex = os.getenv("GOOGLE_CLOUD_PROJECT")
        location_vertex = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        backend_identity = f"vertex:{project_id_vertex}:{location_vertex}" if project_id_vertex else "gemini"
        model_name = f"{caps['model']}|{backend_identity}|{caps['aspect_ratio']}|{caps['resolution']}|1"
        requested_duration = parse_duration_seconds_from_timing(shot.get("timing", "00:00 - 00:05"))
        return _build_input_hash(
            model_name=model_name,
            prompt_hash=prompt_hash,
            source_image_hashes=source_image_hashes,
            requested_duration=requested_duration,
            requested_width=0,
            requested_height=0,
            seed=None,
            frame_types=frame_types,
            provider_name="veo",
        )

    return None


def migrate_provider_jobs_hash(
    project_dir: Path,
    dry_run: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    shots_dir = Path(project_dir) / "97_shots"
    jobs_path = shots_dir / "provider_jobs.json"
    shots_by_key = _load_shots_by_key(shots_dir / "shots.json")

    migrated = 0
    already_current = 0
    skipped_stub = 0
    unrecoverable: List[Dict[str, Any]] = []

    try:
        store = _ProviderJobStore(str(jobs_path))
    except json.JSONDecodeError as exc:
        message = f"{jobs_path} повреждён или пуст: не удалось разобрать JSON ({exc})"
        print(f"Ошибка: {message}")
        return {
            "migrated": 0,
            "already_current": 0,
            "skipped_stub": 0,
            "unrecoverable": [],
            "error": message,
        }

    with store._lock, store._acquire_file_lock():
        store._data = store._load()
        for job in store._data["jobs"]:
            if not isinstance(job, dict):
                continue

            if job.get("hash_inputs_version") == _CURRENT_HASH_INPUTS_VERSION:
                already_current += 1
                continue

            provider = job.get("provider")
            shot_key = job.get("shot_key")

            if not job.get("model") or not job.get("prompt_hash") or not job.get("source_image_hashes"):
                skipped_stub += 1
                if verbose:
                    print(f"[skip:stub] shot_key={shot_key} provider={provider}: пустая запись, пропущена")
                continue

            shot = shots_by_key.get(shot_key)
            if shot is None or not shot.get("timing"):
                if verbose or provider != "aitunnel":
                    print(
                        f"[skip:no-shot] shot_key={shot_key} provider={provider}: "
                        "шот не найден в shots.json или у него нет timing"
                    )
                if provider != "aitunnel":
                    unrecoverable.append({"shot_key": shot_key, "provider": provider})
                continue

            new_hash = _recompute_input_hash(job, shot)
            if new_hash is None:
                print(f"[skip:unknown-provider] shot_key={shot_key} provider={provider}")
                if provider != "aitunnel":
                    unrecoverable.append({"shot_key": shot_key, "provider": provider})
                continue

            if verbose:
                print(
                    f"[migrate] shot_key={shot_key} provider={provider}: "
                    f"{job.get('input_hash')} -> {new_hash}"
                )
            job["input_hash"] = new_hash
            job["hash_inputs_version"] = _CURRENT_HASH_INPUTS_VERSION
            migrated += 1

        if migrated and not dry_run:
            store._save_locked()

    report = {
        "migrated": migrated,
        "already_current": already_current,
        "skipped_stub": skipped_stub,
        "unrecoverable": unrecoverable,
    }
    print(
        f"Мигрировано: {migrated}; уже актуально: {already_current}; "
        f"пропущено (заглушки): {skipped_stub}; не восстановлено: {len(unrecoverable)}"
    )
    if dry_run:
        print("--dry-run: изменения не записаны на диск")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True, help="Каталог проекта storybook (содержит 97_shots/)")
    parser.add_argument("--dry-run", action="store_true", help="Не записывать изменения на диск")
    parser.add_argument("--verbose", action="store_true", help="Печатать каждую пересчитанную/пропущенную запись")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    report = migrate_provider_jobs_hash(
        arguments.project_dir,
        dry_run=arguments.dry_run,
        verbose=arguments.verbose,
    )
    if report.get("error"):
        return 1
    if report["unrecoverable"]:
        for entry in report["unrecoverable"]:
            print(f"[unrecoverable] shot_key={entry['shot_key']} provider={entry['provider']}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
