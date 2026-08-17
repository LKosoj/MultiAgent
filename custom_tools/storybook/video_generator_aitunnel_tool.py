import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from custom_tools.storybook.video_generator_aitunnel_jobs import (
    _PROVIDER_NAME,
    _ProviderJobStore,
    _build_input_hash,
    _hash_source_image,
    _hash_text,
    _new_provider_job,
)
from custom_tools.storybook.video_generator_aitunnel_media import (
    _build_frame_image_payload,
    _build_reference_video_payload,
    _is_url_or_data_url,
    _resolve_frame_dimensions,
    _resolve_model_and_size,
    _should_attach_blockout_reference,
)
from custom_tools.storybook.video_generator_common import (
    _get_aitunnel_video_models,
    parse_duration_seconds_from_timing,
    sync_items_to_memory,
    update_shots_with_descriptions,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.aitunnel.ru/v1"
_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_WAIT_SECONDS = 900
_DEFAULT_POLL_INTERVAL_SECONDS = 15
_DEFAULT_POLL_MAX_TRANSIENT_ERRORS = 5
_DEFAULT_POLL_BACKOFF_CAP_SECONDS = 120


class _PollTransientError(Exception):
    """Transient poll failure (network / non-200) after retry budget; task_id survives."""

    def __init__(self, message: str, task_id: Optional[str] = None):
        super().__init__(message)
        self.task_id = task_id


class _PollTimeoutError(Exception):
    """Poll deadline exceeded while the provider task may still be running."""

    def __init__(self, message: str, task_id: Optional[str] = None):
        super().__init__(message)
        self.task_id = task_id


class _ProviderGenerationError(Exception):
    """Provider reported a genuine terminal failure for the task."""

    def __init__(self, message: str, task_id: Optional[str] = None):
        super().__init__(message)
        self.task_id = task_id


def load_env_file() -> None:
    """Загружает переменные окружения из корневого .env файла проекта."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


load_env_file()

try:
    from utils import translate_prompts_in_items
except Exception:
    translate_prompts_in_items = None


def video_generator_aitunnel_tool(
    session_id: str,
    project_id: Optional[str] = None,
    items: Any = None,
    max_concurrency: int = 2,
    enable: bool = False,
    seed: Optional[int] = None,
    language: str = "en",
    force_update_prompts: bool = False,
    skip_prompt_enhancement: bool = False,
    sample_before_batch: bool = False,
    sample_shot_key: Optional[str] = None,
    generate_blockout: bool = False,
    use_blockout_reference: bool = False,
) -> Dict[str, Any]:
    """
    Генерирует видео через AITUNNEL, сохраняя контракт storybook video tools.

    Совместимость по входу:
    - При наличии project_id читает plots/storybooks/{project_id}/97_shots/shots.json
    - При отсутствии project_id может работать напрямую от items/items JSON string
    - Поддерживает start/end кадры через frame_images[first_frame/last_frame]

    Args:
        session_id: Идентификатор сессии для трассировки выполнения и синхронизации с памятью.
        project_id: Идентификатор проекта storybook; при задании читается shots.json из каталога проекта.
        items: Список шотов или JSON-строка с items, если нет shots.json или нужна явная передача данных.
        max_concurrency: Максимальное число параллельных запросов к API.
        enable: Если True, выполняет генерацию; иначе пропускает фактические вызовы API.
        seed: Опциональный seed для воспроизводимости генерации.
        language: Язык промптов/перевода (например, en).
        force_update_prompts: Принудительно обновить video_prompt независимо от timestamp.
        skip_prompt_enhancement: Пропустить улучшение промпта (только перевод и т.п.).
        sample_before_batch: Если True, обрабатывает только один выбранный/ожидающий шот.
        sample_shot_key: Опциональный ключ шота в формате "scene-shot" для sample режима.
        generate_blockout: Включён ли слой болванок проекта (раздел 11.3).
        use_blockout_reference: Подавать ли ролик болванки видео-референсом в запрос (раздел 11.3).

    Returns:
        Словарь со статусом, сообщением и списком results по шотам.
    """
    enable = _as_bool(enable)
    force_update_prompts = _as_bool(force_update_prompts)
    skip_prompt_enhancement = _as_bool(skip_prompt_enhancement)
    sample_before_batch = _as_bool(sample_before_batch)
    generate_blockout = _as_bool(generate_blockout)
    use_blockout_reference = _as_bool(use_blockout_reference)
    if not enable and not project_id and items is None:
        logger.info("🎬 Генерация видео AITUNNEL отключена (enable=False).")
        return {
            "status": "skipped",
            "message": "Генерация видео отключена",
            "results": [],
        }

    shots_file_path: Optional[str] = None
    items_list: List[Dict[str, Any]] = []

    if project_id:
        shots_file_path = f"plots/storybooks/{project_id}/97_shots/shots.json"
        if os.path.exists(shots_file_path):
            try:
                with open(shots_file_path, "r", encoding="utf-8") as shots_file:
                    shots_data = json.load(shots_file)
                if isinstance(shots_data, dict):
                    items_list = shots_data.get("items", [])
                elif isinstance(shots_data, list):
                    items_list = shots_data
                else:
                    logger.error("❌ Неверная структура данных в shots.json")
                    return _error_result("Неверная структура данных в shots.json")
            except Exception as exc:
                logger.error("❌ Ошибка чтения shots.json: %s", exc)
                return _error_result(f"Ошибка чтения shots.json: {exc}")
        elif items is None:
            if not enable:
                logger.info("🎬 Генерация видео AITUNNEL отключена (enable=False).")
                return {
                    "status": "skipped",
                    "message": "Генерация видео отключена",
                    "results": [],
                }
            logger.warning("⚠️ Файл shots.json не найден: %s", shots_file_path)
            return _error_result(f"Файл shots.json не найден: {shots_file_path}")

    if not items_list and items is not None:
        parsed_items, parse_error = _parse_items_payload(items)
        if parse_error:
            logger.error("❌ Ошибка парсинга items: %s", parse_error)
            return _error_result(parse_error)
        items_list = parsed_items

    if not items_list:
        logger.warning("⚠️ Список items пуст")
        return _error_result("Список items пуст")

    if shots_file_path and project_id:
        logger.info("📝 Этап 1: Анализ и обновление описаний изображений")
        descriptions_updated = update_shots_with_descriptions(
            shots_file_path,
            items_list,
            force_update=force_update_prompts,
            skip_prompt_enhancement=skip_prompt_enhancement,
        )
        if descriptions_updated < 0:
            # Запись shots.json не удалась (см. журнал
            # update_shots_with_descriptions) — items_list в памяти уже
            # содержит обновления, перечитывать диск нельзя: там осталась
            # устаревшая версия, это откатило бы их.
            logger.error("❌ Не удалось сохранить обновлённые описания в %s, используем данные из памяти", shots_file_path)
        elif descriptions_updated:
            logger.info("🔄 Описания обновлены, перезагружаем данные из shots.json")
            try:
                with open(shots_file_path, "r", encoding="utf-8") as shots_file:
                    shots_data = json.load(shots_file)
                items_list = shots_data.get("items", []) if isinstance(shots_data, dict) else shots_data
            except Exception as exc:
                logger.error("❌ Ошибка перезагрузки shots.json после обновления описаний: %s", exc)
                return _error_result(f"Ошибка перезагрузки shots.json: {exc}")

    if not enable:
        logger.info("🎬 Генерация видео AITUNNEL отключена (enable=False).")
        sync_items_to_memory(items, items_list)
        return {
            "status": "skipped",
            "message": "Генерация видео отключена",
            "results": [],
        }

    api_key = os.getenv("AITUNNEL_API_KEY")
    if not api_key:
        logger.error("❌ AITUNNEL_API_KEY не найден в переменных окружения")
        return _error_result("AITUNNEL_API_KEY не найден")
    configured_model = (os.getenv("AITUNNEL_VIDEO_MODEL") or "").strip()
    if not configured_model:
        logger.error("❌ AITUNNEL_VIDEO_MODEL не задан в переменных окружения")
        return _error_result("AITUNNEL_VIDEO_MODEL не задан")

    try:
        model_catalog = _get_aitunnel_video_models()
    except Exception as exc:
        logger.error("❌ Не удалось получить capabilities AITUNNEL: %s", exc)
        return _error_result(f"Не удалось получить список моделей AITUNNEL: {exc}")

    try:
        job_store = _ProviderJobStore(f"plots/storybooks/{project_id}/97_shots/provider_jobs.json") if project_id else None
    except Exception as exc:
        logger.error("❌ Ошибка чтения provider_jobs.json: %s", exc)
        return _error_result(f"Ошибка чтения provider_jobs.json: {exc}")
    video_items = _collect_video_items(items_list, include_existing=job_store is not None)
    if not video_items:
        logger.info("ℹ️ Нет кадров для генерации видео AITUNNEL")
        return _error_result("Нет кадров для обработки")

    remaining = 0
    if sample_before_batch:
        video_items, remaining, sample_error = _select_sample_video_items(video_items, sample_shot_key)
        if sample_error:
            return _error_result(sample_error)
        if not video_items:
            logger.info("ℹ️ Нет ожидающих кадров для sample генерации AITUNNEL")
            return {
                "status": "success",
                "message": "Нет ожидающих кадров для sample генерации AITUNNEL",
                "results": [],
                "stats": {
                    "total": 0,
                    "successful": 0,
                    "failed": 0,
                    "remaining": 0,
                },
            }

    results: List[Dict[str, Any]] = []
    worker_count = max(1, int(max_concurrency or 1))

    for batch_start in range(0, len(video_items), worker_count):
        batch_items = video_items[batch_start:batch_start + worker_count]
        logger.info(
            "🎬 AITUNNEL: пакет %s, видео %s",
            batch_start // worker_count + 1,
            len(batch_items),
        )
        with ThreadPoolExecutor(max_workers=len(batch_items)) as executor:
            future_to_item = {
                executor.submit(
                    _generate_single_video_aitunnel,
                    item,
                    session_id,
                    api_key,
                    model_catalog,
                    configured_model,
                    seed,
                    language,
                    job_store,
                    generate_blockout,
                    use_blockout_reference,
                ): item
                for item in batch_items
            }

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error("❌ Исключение при генерации видео AITUNNEL: %s", exc)
                    result = {
                        "success": False,
                        "error": str(exc),
                        "scene_number": item.get("scene_number"),
                        "shot_number": item.get("shot_number"),
                        "video_path": item.get("video_path"),
                    }
                results.append(result)

    # ТЗ раздел 20.3, код P09: video_generator — единственный, кто знает,
    # какие референсы фактически ушли в запрос; фиксирует результат уже
    # принятого решения (_should_attach_blockout_reference), не дублируя его.
    _write_video_generator_report_section(project_id, results, generate_blockout)

    successful = len([result for result in results if result.get("success")])
    total = len(results)

    sync_items_to_memory(items, items_list)

    stats = {
        "total": total,
        "successful": successful,
        "failed": total - successful,
    }
    if sample_before_batch:
        stats["remaining"] = remaining

    message = f"Сгенерировано {successful} из {total} видео AITUNNEL"
    if successful == total:
        return {
            "status": "success",
            "message": message,
            "results": results,
            "stats": stats,
        }
    return _error_result(message, results=results, stats=stats)


def _parse_items_payload(items: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if isinstance(items, str):
        try:
            items_obj = json.loads(items)
        except Exception as exc:
            return [], f"Невалидный JSON: {exc}"
    else:
        items_obj = items

    if isinstance(items_obj, dict) and isinstance(items_obj.get("items"), list):
        return items_obj["items"], None
    if isinstance(items_obj, list):
        return items_obj, None
    return [], "Неверная структура данных items"


def _error_result(message: str, **extra: Any) -> Dict[str, Any]:
    result = {
        "status": "error",
        "message": message,
        "error": message,
        "results": [],
    }
    result.update(extra)
    return result


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


# P09_SKIPPED_REASON — условие 1 §11.3 (слой болванок/видео-референс выключены
# конфигурацией). ТЗ §20.2: P09 фиксируется только для невыполненных условий
# 2-5 — намеренно выключенный референс не является наблюдением, о котором
# нужно предупреждать.
_P09_SKIPPED_REASON = "condition_1_reference_disabled"


def _write_video_generator_report_section(
    project_id: Optional[str],
    results: List[Dict[str, Any]],
    generate_blockout: bool,
) -> None:
    """ТЗ раздел 20.3: секция ``video_generator`` (код P09) в
    ``93_blockout/report.json``, под той же sidecar-блокировкой, что и у
    остальных писателей отчёта. Пишется последней в графе шагов.

    Создатель файла — только ``blockout_scene_builder``: если файла ещё
    нет, эта функция ничего не создаёт и не падает. Секция пишется только
    при включённом слое болванок (иначе P09 не возникает вовсе, раздел
    20.3); внутри секции — слияние по ключу шота (scene_number,
    shot_number), чужие находки вне scope этого прогона не трогаются.
    """
    if not generate_blockout or not project_id:
        return

    from custom_tools.storybook.blockout_scene_builder import merge_write_report
    from custom_tools.storybook.project_paths import safe_storybook_project_dir

    report_path = safe_storybook_project_dir(project_id) / "93_blockout" / "report.json"
    if not report_path.is_file():
        return

    processed_keys = set()
    findings: List[Dict[str, Any]] = []
    for result in results:
        scene_number = result.get("scene_number")
        shot_number = result.get("shot_number")
        processed_keys.add((scene_number, shot_number))
        reason = result.get("video_reference_rejected_reason")
        if reason and reason != _P09_SKIPPED_REASON:
            findings.append({
                "code": "P09",
                "level": "warning",
                "scene_number": scene_number,
                "shot_number": shot_number,
                "message": f"blockout video reference not attached to the request: {reason}",
                "details": {"reason": reason},
            })

    def _update(section: Dict[str, Any]) -> Dict[str, Any]:
        section = dict(section)
        checks = [
            c for c in (section.get("checks") or [])
            if (c.get("scene_number"), c.get("shot_number")) not in processed_keys
        ]
        checks.extend(findings)
        section["checks"] = checks
        return section

    merge_write_report(report_path, "video_generator", _update)


def _collect_video_items(items_list: List[Dict[str, Any]], include_existing: bool = False) -> List[Dict[str, Any]]:
    video_items: List[Dict[str, Any]] = []
    seen_keys = set()

    for item in items_list:
        shot_type = item.get("shot_type")
        if shot_type and shot_type != "start":
            continue

        video_prompt = str(item.get("video_prompt", "") or "").strip()
        video_path = item.get("video_path")
        scene_number = item.get("scene_number", "?")
        shot_number = item.get("shot_number", "?")

        if not video_prompt or not video_path:
            logger.debug("⏭️ Пропускаем кадр без video_prompt/video_path: %s-%s", scene_number, shot_number)
            continue

        output_exists = _is_non_empty_file(video_path)
        if output_exists and not include_existing:
            logger.info("✅ Видео уже существует: %s", video_path)
            continue

        start_image = item.get("start_image")
        end_image = item.get("end_image")
        video_dir = os.path.dirname(video_path)

        if start_image and not _is_url_or_data_url(start_image) and not os.path.exists(start_image):
            start_image = None
        if end_image and not _is_url_or_data_url(end_image) and not os.path.exists(end_image):
            end_image = None

        if not start_image:
            start_image = _discover_shot_image(video_dir, scene_number, shot_number, "start")
        if not end_image:
            end_image = _discover_shot_image(video_dir, scene_number, shot_number, "end")

        if not start_image and not output_exists:
            logger.debug("⏭️ Стартовый кадр не найден для %s-%s", scene_number, shot_number)
            continue

        shot_key = _shot_key(item)
        if shot_key in seen_keys:
            logger.debug("⏭️ Пропускаем дубликат кадра: %s", shot_key)
            continue

        item_copy = item.copy()
        item_copy["start_image"] = start_image
        item_copy["end_image"] = end_image
        seen_keys.add(shot_key)
        video_items.append(item_copy)

    return video_items


def _select_sample_video_items(
    video_items: List[Dict[str, Any]],
    sample_shot_key: Optional[str],
) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    pending_items = [item for item in video_items if not _is_non_empty_file(str(item.get("video_path") or ""))]

    normalized_key = str(sample_shot_key or "").strip()
    if normalized_key:
        selected = [item for item in video_items if _shot_key(item) == normalized_key]
        if not selected:
            return [], 0, f"sample_shot_key не найден: {normalized_key}"
        selected_item = selected[0]
        remaining = len([item for item in pending_items if _shot_key(item) != normalized_key])
        return [selected_item], remaining, None

    if not pending_items:
        return [], 0, None

    selected_item = pending_items[0]
    remaining = max(0, len(pending_items) - 1)
    return [selected_item], remaining, None


def _shot_key(item: Dict[str, Any]) -> str:
    return f"{item.get('scene_number', '?')}-{item.get('shot_number', '?')}"


def _is_non_empty_file(path: str) -> bool:
    try:
        return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _discover_shot_image(
    video_dir: str,
    scene_number: Any,
    shot_number: Any,
    shot_type: str,
) -> Optional[str]:
    if not video_dir or not os.path.exists(video_dir):
        return None

    try:
        scene_num = int(scene_number) if scene_number != "?" else 1
        shot_num = int(shot_number) if shot_number != "?" else 1
    except (TypeError, ValueError):
        scene_num = 1
        shot_num = 1

    filename = f"img_final_{shot_type}_{scene_num:02d}_{shot_num:02d}.png"
    candidate = os.path.join(video_dir, filename)
    return candidate if os.path.exists(candidate) else None


def _requested_frame_dimensions(item: Dict[str, Any]) -> Tuple[int, int]:
    """User-requested width/height (catalog-independent) for the M-6 input hash."""
    try:
        return int(item.get("width") or 0), int(item.get("height") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _extract_cost_and_currency(status_payload: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
    usage = status_payload.get("usage") or {}
    if "cost" in usage:
        return usage.get("cost"), usage.get("currency")
    if "cost_rub" in usage:
        return usage.get("cost_rub"), "RUB"
    return None, None


def _generate_single_video_aitunnel(
    item: Dict[str, Any],
    session_id: str,
    api_key: str,
    model_catalog: Dict[str, Dict[str, Any]],
    configured_model: str,
    seed: Optional[int],
    language: str,
    job_store: Optional[_ProviderJobStore] = None,
    generate_blockout: bool = False,
    use_blockout_reference: bool = False,
) -> Dict[str, Any]:
    del session_id

    scene_number = item.get("scene_number", "?")
    shot_number = item.get("shot_number", "?")
    shot_key = _shot_key(item)
    video_path = item.get("video_path")
    start_image = item.get("start_image")
    end_image = item.get("end_image")
    input_hash: Optional[str] = None
    # ТЗ раздел 20.2/20.3, код P09: причина отказа условий 2-5 §11.3, если
    # видео-референс болванки не был подан (см. _should_attach_blockout_reference
    # ниже). Предобъявлено, чтобы быть доступным и в except-обработчике.
    video_reference_rejected_reason: Optional[str] = None

    try:
        prompt = _resolve_video_prompt(item, language)
        if not prompt:
            raise ValueError("Пустой video_prompt")

        prompt_hash = _hash_text(prompt)
        source_image_hashes = {
            "start_image": _hash_source_image(start_image),
            "end_image": _hash_source_image(end_image),
        }
        output_exists = _is_non_empty_file(str(video_path or ""))
        trusted_output_job = (
            job_store.find_latest_output_job_for_shot(shot_key, str(video_path))
            if job_store and output_exists
            else None
        )
        trusted_source_hashes = trusted_output_job.get("source_image_hashes") if trusted_output_job else {}
        missing_trusted_source_hash = any(
            source_image_hashes.get(name) is None and bool((trusted_source_hashes or {}).get(name))
            for name in source_image_hashes
        )
        if trusted_output_job and missing_trusted_source_hash:
            job_store.update_job(
                trusted_output_job["shot_key"],
                trusted_output_job["input_hash"],
                {
                    "status": "downloaded",
                    "output_path": str(video_path),
                    "error": None,
                },
                "downloaded_at",
            )
            return {
                "success": True,
                "video_path": video_path,
                "scene_number": scene_number,
                "shot_number": shot_number,
                "task_id": trusted_output_job.get("task_id"),
                "video_url": trusted_output_job.get("video_url"),
                "error": None,
                "model": trusted_output_job.get("model") or configured_model,
                "cost_rub": trusted_output_job.get("cost") if trusted_output_job.get("currency") == "RUB" else None,
                "cost": trusted_output_job.get("cost"),
                "currency": trusted_output_job.get("currency"),
            }
        requested_duration = _parse_duration_from_timing(item.get("timing", "00:00 - 00:06"))
        requested_width, requested_height = _requested_frame_dimensions(item)
        frame_types = ["first_frame"] + (["last_frame"] if end_image else [])
        model_name = configured_model
        size_params: Dict[str, str] = {}
        duration = requested_duration

        try:
            width, height = _resolve_frame_dimensions(item, start_image)
            model_name, size_params, duration = _resolve_model_and_size(
                model_catalog=model_catalog,
                configured_model=configured_model,
                width=width,
                height=height,
                duration=requested_duration,
                requires_last_frame=bool(end_image),
                seed=seed,
            )
        except Exception:
            if not output_exists:
                raise

        # Раздел 11.3: подавать ли ролик болванки видео-референсом. Пять условий
        # проверяются здесь, до формирования хеша и записи журнала задания.
        should_attach_reference, reference_info = _should_attach_blockout_reference(
            item, generate_blockout, use_blockout_reference, duration, size_params
        )
        reference_video_path = reference_info if should_attach_reference else None
        video_reference_rejected_reason = None if should_attach_reference else reference_info
        reference_video_hash = _hash_source_image(reference_video_path) if reference_video_path else None

        # M-6: hash user inputs only; the catalog-resolved model/size/duration are
        # stored as job metadata below, never fed into input_hash.
        # Note: duration normalization in 97_shots rewrites `timing`/`duration_s` before
        # requested_duration is derived, so it still flows in via user input and does not
        # violate M-6. A model-catalog change can still trigger regeneration for affected
        # shots — now with an explicit P16 warning + shot list instead of silently.
        # Э8 (раздел 11.3): reference_video_hash входит в словарь только когда референс
        # действительно подан — иначе состав словаря и хеш остаются прежними (A31/A37).
        input_hash = _build_input_hash(
            model_name=configured_model,
            prompt_hash=prompt_hash,
            source_image_hashes=source_image_hashes,
            requested_duration=requested_duration,
            requested_width=requested_width,
            requested_height=requested_height,
            seed=seed,
            frame_types=frame_types,
            reference_video_hash=reference_video_hash,
        )

        if job_store:
            job_store.mark_stale_for_changed_input(shot_key, input_hash)
            existing_job = job_store.find_current_job(shot_key, input_hash)
            has_prior_job_for_shot = job_store.has_job_for_shot(shot_key)
            job_store.ensure_job(
                _new_provider_job(
                    shot_key=shot_key,
                    model=model_name,
                    prompt_hash=prompt_hash,
                    source_image_hashes=source_image_hashes,
                    input_hash=input_hash,
                    output_path=str(video_path),
                    resolved_size_params=size_params,
                    resolved_duration=duration,
                    video_reference=reference_video_path,
                    video_reference_rejected_reason=video_reference_rejected_reason,
                )
            )
        else:
            existing_job = None
            has_prior_job_for_shot = False

        if output_exists and (not job_store or existing_job or not has_prior_job_for_shot):
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {
                        "status": "downloaded",
                        "model": model_name,
                        "prompt_hash": prompt_hash,
                        "source_image_hashes": source_image_hashes,
                        "output_path": str(video_path),
                        "error": None,
                    },
                    "downloaded_at",
                )
            return {
                "success": True,
                "video_path": video_path,
                "scene_number": scene_number,
                "shot_number": shot_number,
                "task_id": None,
                "video_url": None,
                "error": None,
                "model": model_name,
                "cost_rub": None,
                "cost": None,
                "currency": None,
                "video_reference_rejected_reason": video_reference_rejected_reason,
            }

        frame_images = [_build_frame_image_payload(start_image, "first_frame")]
        if end_image:
            frame_images.append(_build_frame_image_payload(end_image, "last_frame"))

        payload: Dict[str, Any] = {
            "model": model_name,
            "prompt": prompt,
            "duration": duration,
            "frame_images": frame_images,
            "generate_audio": False,
        }
        payload.update(size_params)
        if seed is not None:
            payload["seed"] = seed
        if reference_video_path:
            payload["reference_video"] = _build_reference_video_payload(reference_video_path)

        video_dir = os.path.dirname(video_path)
        if video_dir:
            os.makedirs(video_dir, exist_ok=True)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        base_url = os.getenv("AITUNNEL_API_BASE", _DEFAULT_BASE_URL).rstrip("/")
        poll_callback = (
            (lambda payload: _record_poll_update(job_store, shot_key, input_hash, payload))
            if job_store
            else None
        )

        resume_job = (
            job_store.find_resumable_job(shot_key, input_hash, prompt_hash, source_image_hashes)
            if job_store
            else None
        )
        status_payload: Dict[str, Any] = {}
        task_id = None
        video_url = None
        video_url_requires_auth = False

        if resume_job and (resume_job.get("task_id") or resume_job.get("video_url")):
            task_id = resume_job.get("task_id")
            video_url = resume_job.get("video_url")
            video_url_requires_auth = _stored_video_url_requires_auth(resume_job, str(video_url or ""), base_url)
            status = resume_job.get("status")
            if status == "download_failed" and task_id:
                # M-14: the stored URL may be a signed CDN link that has since expired;
                # re-poll the task_id for a fresh unsigned URL instead of hammering the
                # stale one forever.
                status_payload = _wait_for_video_completion_aitunnel(
                    task_id=task_id,
                    headers=headers,
                    base_url=base_url,
                    on_poll=poll_callback,
                )
                unsigned_urls = status_payload.get("unsigned_urls") or []
                video_url = unsigned_urls[0] if unsigned_urls else f"{base_url}/videos/{task_id}/content?index=0"
                video_url_requires_auth = not bool(unsigned_urls)
            elif video_url and status in {"completed", "download_failed", "downloaded"}:
                # download_failed without task_id: reuse the previously stored URL.
                status_payload = {"usage": {}}
            elif task_id:
                status_payload = _wait_for_video_completion_aitunnel(
                    task_id=task_id,
                    headers=headers,
                    base_url=base_url,
                    on_poll=poll_callback,
                )
                unsigned_urls = status_payload.get("unsigned_urls") or []
                video_url = unsigned_urls[0] if unsigned_urls else f"{base_url}/videos/{task_id}/content?index=0"
                video_url_requires_auth = not bool(unsigned_urls)
            else:
                raise RuntimeError("В provider_jobs.json нет task_id или video_url для resume")
        else:
            if resume_job and resume_job.get("status") in {"submitting", "submitted", "pending", "in_progress"}:
                raise RuntimeError(
                    "Найдена неоднозначная provider job без task_id/video_url; "
                    "повторная отправка заблокирована, чтобы не создать дубликат платной задачи"
                )
            # H-3 (money-critical): a prior job for these inputs may be marked failed while
            # its paid task_id actually completed at the provider. Before spending on a
            # resubmit, verify the old task_id with a single free GET and adopt it if done.
            old_task_id = (
                job_store.find_task_id_for_resubmit(shot_key, input_hash, prompt_hash, source_image_hashes)
                if job_store
                else None
            )
            adopted_payload = (
                _verify_completed_task_before_resubmit(old_task_id, headers, base_url)
                if old_task_id
                else None
            )
            if adopted_payload is not None:
                task_id = old_task_id
                status_payload = adopted_payload
                if job_store:
                    job_store.update_job(
                        shot_key,
                        input_hash,
                        {
                            "task_id": task_id,
                            "status": "submitted",
                            "model": model_name,
                            "prompt_hash": prompt_hash,
                            "source_image_hashes": source_image_hashes,
                            "output_path": str(video_path),
                            "error": None,
                        },
                        "submitted_at",
                    )
            else:
                if job_store:
                    job_store.update_job(
                        shot_key,
                        input_hash,
                        {
                            "status": "submitting",
                            "model": model_name,
                            "prompt_hash": prompt_hash,
                            "source_image_hashes": source_image_hashes,
                            "output_path": str(video_path),
                            "error": None,
                        },
                        "submitting_at",
                    )
                submit_response = requests.post(
                    f"{base_url}/videos",
                    headers=headers,
                    json=payload,
                    timeout=_DEFAULT_TIMEOUT_SECONDS,
                )
                if submit_response.status_code not in (200, 202):
                    raise RuntimeError(
                        f"AITUNNEL submit failed: {submit_response.status_code} - {submit_response.text}"
                    )

                submit_payload = submit_response.json()
                task_id = submit_payload.get("id")
                if not task_id:
                    raise RuntimeError(f"Не получен id задачи от AITUNNEL: {submit_payload}")

                if job_store:
                    job_store.update_job(
                        shot_key,
                        input_hash,
                        {
                            "task_id": task_id,
                            "status": submit_payload.get("status") or "submitted",
                            "model": model_name,
                            "prompt_hash": prompt_hash,
                            "source_image_hashes": source_image_hashes,
                            "output_path": str(video_path),
                            "error": None,
                        },
                        "submitted_at",
                    )

                status_payload = _wait_for_video_completion_aitunnel(
                    task_id=task_id,
                    headers=headers,
                    base_url=base_url,
                    on_poll=poll_callback,
                )
            unsigned_urls = status_payload.get("unsigned_urls") or []
            video_url = unsigned_urls[0] if unsigned_urls else f"{base_url}/videos/{task_id}/content?index=0"
            video_url_requires_auth = not bool(unsigned_urls)

        cost, currency = _extract_cost_and_currency(status_payload)
        if cost is None and resume_job:
            cost = resume_job.get("cost")
            currency = resume_job.get("currency")
        if job_store:
            job_store.update_job(
                shot_key,
                input_hash,
                {
                    "task_id": task_id,
                    "status": "completed",
                    "cost": cost,
                    "currency": currency,
                    "video_url": video_url,
                    "video_url_requires_auth": video_url_requires_auth,
                    "error": None,
                },
                "completed_at",
            )

        if job_store:
            job_store.update_job(shot_key, input_hash, {"status": "downloading", "error": None}, None)
        try:
            _download_video_aitunnel(
                video_url,
                video_path,
                _download_headers(video_url, headers, base_url, video_url_requires_auth),
            )
        except Exception as exc:
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {"status": "download_failed", "error": str(exc), "video_url": video_url},
                    "failed_at",
                )
            raise

        if job_store:
            job_store.update_job(
                shot_key,
                input_hash,
                {
                    "status": "downloaded",
                    "cost": cost,
                    "currency": currency,
                    "video_url": video_url,
                    "video_url_requires_auth": video_url_requires_auth,
                    "output_path": str(video_path),
                    "error": None,
                },
                "downloaded_at",
            )

        return {
            "success": True,
            "video_path": video_path,
            "scene_number": scene_number,
            "shot_number": shot_number,
            "task_id": task_id,
            "video_url": video_url,
            "error": None,
            "model": model_name,
            "cost_rub": cost if currency == "RUB" else ((status_payload.get("usage") or {}).get("cost_rub")),
            "cost": cost,
            "currency": currency,
            "video_reference_rejected_reason": video_reference_rejected_reason,
        }
    except Exception as exc:
        if job_store and input_hash:
            current_job = job_store.find_current_job(shot_key, input_hash)
            if current_job and current_job.get("status") == "download_failed":
                status = "download_failed"
            elif isinstance(exc, (_PollTransientError, _PollTimeoutError)) and getattr(exc, "task_id", None):
                # H-3: transient/timeout poll failures keep a live task_id — mark
                # resumable (poll_timeout), never failed, or the paid task is lost.
                status = "poll_timeout"
            else:
                status = "failed"
            job_store.update_job(shot_key, input_hash, {"status": status, "error": str(exc)}, "failed_at")
        logger.error("❌ AITUNNEL: ошибка генерации %s-%s: %s", scene_number, shot_number, exc)
        return {
            "success": False,
            "error": str(exc),
            "scene_number": scene_number,
            "shot_number": shot_number,
            "video_path": video_path,
            "video_reference_rejected_reason": video_reference_rejected_reason,
        }


def _resolve_video_prompt(item: Dict[str, Any], language: str) -> str:
    if language == "en" or translate_prompts_in_items is None:
        return str(item.get("video_prompt", "") or "").strip()

    translated_item = translate_prompts_in_items(item, "en")
    return str(translated_item.get("video_prompt", item.get("video_prompt", "")) or "").strip()


def _record_poll_update(
    job_store: _ProviderJobStore,
    shot_key: str,
    input_hash: str,
    payload: Dict[str, Any],
) -> None:
    # M-13: record the raw provider status in provider_status only. The internal
    # lifecycle status is driven exclusively by explicit transitions, so a novel
    # provider status can never masquerade as a terminal internal failure.
    updates: Dict[str, Any] = {
        "provider_status": payload.get("status") or "unknown",
        "error": payload.get("error"),
    }
    cost, currency = _extract_cost_and_currency(payload)
    if cost is not None:
        updates["cost"] = cost
        updates["currency"] = currency
    job_store.update_job(shot_key, input_hash, updates, "polled_at")


def _poll_backoff_seconds(attempt: int, base_interval: int) -> float:
    return min(base_interval * (2 ** max(0, attempt - 1)), _DEFAULT_POLL_BACKOFF_CAP_SECONDS)


def _wait_for_video_completion_aitunnel(
    task_id: str,
    headers: Dict[str, str],
    base_url: str,
    max_wait_time: int = _DEFAULT_MAX_WAIT_SECONDS,
    poll_interval: int = _DEFAULT_POLL_INTERVAL_SECONDS,
    on_poll: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    start_time = time.time()
    transient_errors = 0

    while time.time() - start_time < max_wait_time:
        # H-3: network/non-200 failures are transient — retry with capped backoff
        # inside the deadline instead of failing the job and losing the task_id.
        try:
            response = requests.get(
                f"{base_url}/videos/{task_id}",
                headers=headers,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            transient_errors += 1
            if transient_errors > _DEFAULT_POLL_MAX_TRANSIENT_ERRORS:
                raise _PollTransientError(
                    f"AITUNNEL polling network errors exceeded budget for task {task_id}: {exc}",
                    task_id=task_id,
                )
            time.sleep(_poll_backoff_seconds(transient_errors, poll_interval))
            continue

        if response.status_code != 200:
            transient_errors += 1
            if transient_errors > _DEFAULT_POLL_MAX_TRANSIENT_ERRORS:
                raise _PollTransientError(
                    f"AITUNNEL polling failed for task {task_id}: "
                    f"{response.status_code} - {response.text}",
                    task_id=task_id,
                )
            time.sleep(_poll_backoff_seconds(transient_errors, poll_interval))
            continue

        transient_errors = 0
        payload = response.json()
        status = payload.get("status")
        if on_poll:
            on_poll(payload)
        if status == "completed":
            return payload
        if status == "failed":
            raise _ProviderGenerationError(
                f"AITUNNEL generation failed: {payload.get('error')}",
                task_id=task_id,
            )
        # M-13: any other status (queued/moderation/unknown/pending/in_progress) is
        # treated as still-in-progress until the deadline, not an instant failure.
        time.sleep(poll_interval)

    raise _PollTimeoutError(
        f"Превышено время ожидания AITUNNEL task {task_id}",
        task_id=task_id,
    )


def _verify_completed_task_before_resubmit(
    task_id: str,
    headers: Dict[str, str],
    base_url: str,
) -> Optional[Dict[str, Any]]:
    """One free GET of an old task_id before paying for a resubmit (H-3).

    Returns the status payload only if the provider reports the task already
    completed (adopt it, no new submit). Returns None on genuine failure,
    in-progress, or transient errors so the caller submits a fresh task — a
    genuinely-failed task is never resurrected.
    """
    try:
        response = requests.get(
            f"{base_url}/videos/{task_id}",
            headers=headers,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    payload = response.json()
    if payload.get("status") == "completed":
        return payload
    return None


def _download_video_aitunnel(video_url: str, output_path: str, headers: Dict[str, str]) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output.with_name(f".{output.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    response = requests.get(
        video_url,
        headers=headers,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        stream=True,
    )
    if response.status_code != 200:
        raise RuntimeError(f"AITUNNEL download failed: {response.status_code} - {response.text}")

    try:
        with tmp_path.open("wb") as output_file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())

        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            raise RuntimeError(f"Пустой файл после скачивания видео: {output_path}")
        os.replace(tmp_path, output)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError(f"Пустой файл после скачивания видео: {output_path}")


def _download_headers(
    video_url: str,
    api_headers: Dict[str, str],
    base_url: str,
    requires_auth: bool,
) -> Dict[str, str]:
    if not requires_auth:
        return {}
    if not str(video_url or "").startswith(base_url.rstrip("/") + "/"):
        return {}
    return {"Authorization": api_headers.get("Authorization", "")}


def _stored_video_url_requires_auth(job: Dict[str, Any], video_url: str, base_url: str) -> bool:
    stored = job.get("video_url_requires_auth")
    if stored is not None:
        return bool(stored)
    return str(video_url or "").startswith(base_url.rstrip("/") + "/")


def _parse_duration_from_timing(timing: str) -> int:
    return int(parse_duration_seconds_from_timing(timing))
