import json
import os
import logging
import re
import random
import threading
import fcntl  # Для блокировки файлов
from typing import Any, Dict, List, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from agent_command import model_hard, model_code, model_ultimate, model_lite
from utils import call_openai_api, parse_llm_json


# Импорты из модуля screenplay_shots_generator_utils
from .screenplay_shots_generator_utils import (
    _generate_fcpxml, _generate_photo_fcpxml,
    _build_extended_context,
    _analyze_shot_technical, _analyze_end_shot_technical,
    _create_shot_item,
)
from .screenplay_shots_generator_utils.shared_utils import (
    enrich_shot_frame_spec_environment_delta_via_llm,
    black_screen_storyboard_shot,
)

logger = logging.getLogger(__name__)

# Пороги масштабирования END кадра. Значения согласованы с system_prompt'ами
# (`Ratio ≈1.0 (0.98–1.02) → НЕ генерируй команду масштабирования`,
#  ШАГ 8 в _generate_end_shot_artistic). Раньше код использовал 0.95/1.05,
# что создавало dead zone между [0.95, 0.98) и (1.02, 1.05] — ratio в этих
# диапазонах ни в zoom, ни в no-change. Теперь пороги совпадают с промптом.
RATIO_NO_CHANGE_UPPER = 1.02
RATIO_NO_CHANGE_LOWER = 0.98

_PROMPT_LANGUAGE_LABELS = {
    "ru": "русском языке",
    "en": "английском языке",
    "es": "испанском языке",
    "fr": "французском языке",
    "de": "немецком языке",
}
_PROMPT_EDIT_PREFIXES = {
    "ru": "Редактируй image 1:",
    "en": "Edit image 1:",
    "es": "Edita la imagen 1:",
    "fr": "Modifie l'image 1:",
    "de": "Bearbeite Bild 1:",
}
_PROMPT_CREATE_PREFIXES = {
    "ru": "Создай",
    "en": "Create",
    "es": "Crea",
    "fr": "Cree",
    "de": "Erstelle",
}


def _get_prompt_language_label(language: str) -> str:
    return _PROMPT_LANGUAGE_LABELS.get(language, f"языке с кодом {language}")


def _get_prompt_edit_prefix(language: str) -> str:
    return _PROMPT_EDIT_PREFIXES.get(language, _PROMPT_EDIT_PREFIXES["en"])


def _get_prompt_create_prefix(language: str) -> str:
    return _PROMPT_CREATE_PREFIXES.get(language, _PROMPT_CREATE_PREFIXES["en"])


def _allow_end_location_change_via_llm(
    *,
    start_location_name: str,
    candidate_end_location_name: str,
    shot_description: str,
    scene_action: str,
    shot_frame_spec: Optional[Dict[str, Any]],
) -> bool:
    """
    Решает через model_lite, допустима ли смена локации между START и END этого же шота.
    Возвращает True только при явном подтверждении.
    """
    start_loc = str(start_location_name or "").strip()
    end_loc = str(candidate_end_location_name or "").strip()
    if not start_loc or not end_loc or start_loc.casefold() == end_loc.casefold():
        return False

    spec = shot_frame_spec if isinstance(shot_frame_spec, dict) else {}
    transition = spec.get("transition_spec") if isinstance(spec.get("transition_spec"), dict) else {}
    env_delta = transition.get("environment_delta")
    if not isinstance(env_delta, list):
        env_delta = []

    prompt = (
        "Определи, есть ли ЯВНАЯ смена локации между START и END в пределах одного шота.\n"
        "Разрешай смену локации только если это явно подтверждено shot_description/scene_action/"
        "transition_spec.environment_delta.\n"
        "Движение камеры/параллакс/раскрытие пространства внутри той же локации НЕ является сменой локации.\n\n"
        f"start_location_name: {json.dumps(start_loc, ensure_ascii=False)}\n"
        f"candidate_end_location_name: {json.dumps(end_loc, ensure_ascii=False)}\n"
        f"shot_description: {json.dumps(str(shot_description or ''), ensure_ascii=False)}\n"
        f"scene_action: {json.dumps(str(scene_action or ''), ensure_ascii=False)}\n"
        f"transition_environment_delta: {json.dumps(env_delta, ensure_ascii=False)}\n\n"
        "Верни только JSON:\n"
        "{\"allow_location_change\": true|false, \"reason\": \"short\"}"
    )
    try:
        resp = call_openai_api(
            prompt=prompt,
            system_prompt=(
                "Ты валидатор continuity локации в storyboard. "
                "Консервативное правило: без явного подтверждения смена локации запрещена."
            ),
            model=model_lite,
            max_tokens=200,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        obj = parse_llm_json(resp)
        return bool((obj or {}).get("allow_location_change"))
    except Exception as e:
        logger.warning("END location-change LLM check failed: %s", e)
        return False


def _build_state_to_image_common_core(
    *,
    phase_name: str,
    state_spec_name: str,
    composition_field: str,
    camera_position_field: str,
    orientation_field: str,
    summary_field: str,
) -> str:
    """
    Общий универсальный core для генерации image-prompt'ов по состоянию кадра.
    START и END должны пользоваться одной и той же логикой literal/frame-first описания,
    различаясь только source-of-truth для своей фазы.
    """
    return f"""
**[P1 CRITICAL] UNIVERSAL STATE-TO-IMAGE CORE ({phase_name}):**
- Генерируй literal image-edit prompt для одного видимого кадра, а не литературный пересказ или интерпретацию сцены.
- Source of truth для этой фазы: `shot_frame_spec` + `{state_spec_name}` + `world_physics` + текущие structured fields.
- Описывай только то, что действительно должно быть видно в этой фазе; не смешивай раннее и позднее состояния одного и того же шота.
- `{summary_field}`, `{composition_field}`, `{camera_position_field}`, `{orientation_field}` и `english_prompt` должны описывать ОДИН И ТОТ ЖЕ кадр.
- Предпочитай конкретные видимые факты: положение тела, контакты, зазоры, перекрытия, разрыв поверхности, направление взгляда, читаемое выражение лица.
- Если в source-of-truth есть `pose_signature`, это и есть главный источник для читаемой позы/жеста: корпус, голова, руки, кисти, реквизит и их взаимное положение.
- Не схлопывай `pose_signature` в generic standing / sitting / resting posture. Статика допустима, но должна сохранять выразительную геометрию жеста.
- Даёшь только минимально необходимую физическую конкретику. Не раздувай prompt до механического перечня всех точек контакта, если они не критичны для читаемости кадра.
- Если субъект и видимая опора / край проёма / разрыв поверхности причинно связаны в этом кадре, держи их как один композиционный узел.
  Нельзя сдвигать субъекта в сторону только ради того, чтобы отдельно показать опору или отверстие.
  Если более тесный кроп разрушает эту причинную связь, кадр должен стать на один шаг шире, а не терять физическую читаемость.
- Не заменяй видимые факты декоративной авторской прозой, метафорами или "красивыми" обобщениями.
- Описывай внешность персонажа через прямые канонические признаки из референсов и описаний, а не через сравнения, метафоры, зооморфные аналогии или "как у ...".
- Используй только визуальный канон персонажа: лицо, глаза, пропорции, прическа, одежда, аксессуары, устойчивые видимые признаки. Не вытаскивай в image-prompt профессию, роль в сюжете, биографию, характер или речевые паттерны, если они не выражены визуально.
- Не добавляй абстрактные mood-labels вроде "торжественность", "роковая пауза", "напряжённая тишина", если они не переведены в видимые признаки света, воздуха, материала, позы или лица.
- Не расширяй style anchors сверх уже заданного проектного стиля. Не добавляй новых художников, школ, эстетик или художественных сравнений, если их нет во входных данных этого шота.
- Не допускай языкового мусора: prompt должен оставаться на языке генерации, кроме допустимых технических camera/lens терминов и явно авторизованных readable texts.
- Не default'и к `facing_camera`, blank neutral face или abstract pose. Такие состояния допустимы только если они явно следуют из storyboard / camera plan / state spec.
- `camera_position` и `orientation` должны быть геометрически совместимы. Не пиши одновременно вид "сзади" и фронтальный/трёхчетвертной обзор лица, если этого не допускает сама схема кадра.
- Если лицо читается, expression и gaze должны следовать из `{state_spec_name}` и `world_physics`; если поле в spec пустое, делай минимальный физически правдоподобный вывод, а не нейтральный reset.
- Если мир кадра физически нестабилен, prompt не должен описывать эмоционально пустое лицо и спокойную постановочную стойку без явного основания в source-of-truth.
- Если `shot_frame_spec`, `{state_spec_name}` или `prop_continuity` явно называют реквизит/аксессуар, сохраняй его предметную идентичность дословно. Не подменяй трость стулом, книгу коробкой, очки маской и т.п. только из-за формы, материала или ассоциаций.
- Если в кадре несколько персонажей, держи их визуальные якоря раздельно: не смешивай лицо, одежду, аксессуары и силуэт одного персонажа с другим. Используй `shot_role` и `must_remain_distinct_from` из per-shot cast data как обязательные ограничения различимости.
- Если per-shot cast data содержит `phase_pose_signature` для персонажа, это authoritative поза именно этого персонажа в текущей фазе. Не позволяй позе primary subject перетекать на secondary characters и наоборот.
- Не добавляй новые props, зоны, фоны, символы или декоративные объекты только ради "более красивого" кадра.
- Держи формулировки суше и визуальнее: меньше оценочных фраз, больше буквальных наблюдаемых признаков кадра.
- START и END — это две фазы одного и того же механизма описания кадра, а не два разных авторских стиля.
"""


def _build_state_to_image_phase_rule(phase_name: str) -> str:
    if str(phase_name).upper() == "START":
        return """
- START = самый ранний читаемый кадр этого же шота, а не отдельный спокойный пролог.
- Если внутри шота уже начинается физически значимое изменение, START должен сохранять раннюю читаемую напряжённость этого же события, но без подмены на END-результат.
- Не преувеличивай устойчивость и безопасность кадра, если shot_description/transition/world_physics задают imminent change внутри того же шота.
"""
    return """
- END = финальный стоп-кадр этого же шота, без process prose и без возврата к раннему безопасному состоянию.
- Описывай уже видимый итог опоры, кадрирования, лица и взгляда; не застревай между START и END.
"""


def _build_state_to_image_user_core(
    *,
    phase_name: str,
    state_spec_name: str,
    summary_field: str,
    composition_field: str,
    camera_position_field: str,
    orientation_field: str,
    shot_size_field_name: str,
) -> str:
    """
    Общие пользовательские требования для START/END.
    """
    return f"""
ОБЩИЕ ПРАВИЛА ДЛЯ {phase_name}:
- Используй один и тот же универсальный подход literal state-to-image: опиши конкретный видимый кадр, а не интерпретацию или мини-рассказ.
- `shot_frame_spec` и `{state_spec_name}` — source of truth; не смешивай факты из другой фазы этого же шота.
- `english_prompt`, `{summary_field}`, `{composition_field}`, `{camera_position_field}` и `{orientation_field}` должны быть согласованы между собой.
- Пиши визуально наблюдаемые факты: где субъект, на чём держится, что видно по краям кадра, куда направлен взгляд, как читается лицо, какая часть среды осталась в кадре.
- Если `pose_signature` задан, явно переведи его в кадр: сохрани читаемый жест, наклон корпуса, положение головы, направление рук и отношение реквизита к телу.
- Давай только минимально нужную физическую конкретику. Не превращай prompt в инженерный протокол контактов, если кадр от этого не выигрывает.
- Избегай декоративных литературных формулировок и авторского тона. Предпочитай буквальное описание кадра.
- Описывай персонажа прямыми каноническими признаками. Не используй сравнения вида "усы как у ...", "глаза как ...", "лицо словно ...".
- Используй только визуальные признаки персонажа. Не превращай `role`, биографию, профессию, речевые особенности или сюжетные ярлыки в часть image-prompt, если они не материализованы в самом кадре.
- Не используй абстрактные атмосферные ярлыки, если они не выражены через видимые свойства света, воздуха, пространства, лица или позы.
- Не придумывай новые style references и новые artist names. Используй только те стилевые якоря, которые уже даны во входных данных проекта/шота.
- Не смешивай языки и скрипты. В image prompt не должно быть случайных инородных фрагментов, кроме стандартных технических camera/lens терминов.
- Если shot/frame требует эмоциональной или физической реакции, описывай её как видимое состояние лица и тела, а не как нейтральную заглушку.
- `facing_camera` и прямой взгляд в объектив допустимы только при явном direct-address/frontal-lens кадре.
- Держи геометрию кадра непротиворечивой: `camera_position`, `orientation` и текстовое описание ракурса не должны спорить друг с другом.
- Если реквизит уже назван в `shot_frame_spec`, `{state_spec_name}` или `prop_continuity`, используй то же имя предмета и не заменяй его семантически похожим объектом.
- Если в per-shot cast присутствуют несколько персонажей, используй их как отдельные визуальные сущности: один `primary_subject`, остальные `secondary_visible_character`, без смешения внешних признаков.
- Начинай `english_prompt` с корректной инструкции редактирования для `{shot_size_field_name}` и сохраняй кадр как одну непрерывную иллюстрацию, без split/inset/cutaway.
{_build_state_to_image_phase_rule(phase_name)}
"""

def screenplay_shots_generator_tool(
    session_id: str,
    project_id: str,
    generate_end_shots: bool = True,
    enable: bool = True,
    language: str = 'en',
    max_scenes: Optional[int] = None,
    scene_numbers: Optional[List[int]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Генерирует shots.json для создания изображений кадров на основе режиссерского сценария.
    
    Args:
        session_id: Идентификатор сессии для трассировки выполнения.
        project_id: Идентификатор проекта.
        generate_end_shots: Если True, генерирует end кадры, иначе пропускает выполнение.
        enable: Если True, генерирует shots.json, иначе пропускает выполнение.
        language: Язык генерации промптов для shots.json (по умолчанию 'en').
        max_scenes: Опционально ограничить количество сцен (после фильтрации по scene_numbers).
        scene_numbers: Опционально явный список номеров сцен для генерации (например: [2, 3]).
        force: Если True — игнорирует существующий shots.json и перегенерирует заново (полезно при частичной генерации).
        
    Returns:
        Словарь в формате items.json для совместимости с artist_agent_batch_edit_tool.
    """
    
    if not enable:
        logger.info("🎬 Генерация кадров отключена (enable=False)")
        # Возвращаем пустую структуру в формате items.json
        return {"items": [], "consistency_rules": []}
    
    # Пути к файлам
    screenplay_path = f"plots/storybooks/{project_id}/91_screenplay/screenplay.json"
    characters_path = f"plots/storybooks/{project_id}/20_bible/characters.json"
    locations_path = f"plots/storybooks/{project_id}/20_bible/locations.json"
    consistency_rules_path = f"plots/storybooks/{project_id}/20_bible/consistency_rules.json"
    style_images_path = f"plots/storybooks/{project_id}/30_style/style_images.json"
    
    # Проверяем существование файлов
    for path in [screenplay_path, characters_path, locations_path]:
        if not os.path.exists(path):
            logger.error(f"❌ Не найден файл: {path}")
            return {"items": [], "consistency_rules": [], "error": f"File not found: {path}"}
    
    # Читаем данные
    try:
        with open(screenplay_path, "r", encoding="utf-8") as f:
            screenplay_data = json.load(f)
        
        with open(characters_path, "r", encoding="utf-8") as f:
            characters_data = json.load(f)
        
        with open(locations_path, "r", encoding="utf-8") as f:
            locations_data = json.load(f)
        
        consistency_rules = []
        if os.path.exists(consistency_rules_path):
            with open(consistency_rules_path, "r", encoding="utf-8") as f:
                consistency_rules = json.load(f)

        # Читаем style_images.json (если есть) — это MUST для консистентного визуального стиля
        style_images_data: Dict[str, Any] = {}
        if os.path.exists(style_images_path):
            try:
                with open(style_images_path, "r", encoding="utf-8") as f:
                    style_images_data = json.load(f) or {}
            except Exception as e:
                logger.warning(f"⚠️ Не удалось прочитать style_images.json: {e}")
                style_images_data = {}
        else:
            style_images_data = {}
                
    except Exception as e:
        logger.error(f"❌ Ошибка чтения файлов: {e}")
        return {"items": [], "consistency_rules": [], "error": f"File reading error: {e}"}
    
    # Получаем сцены из screenplay
    screenplay_scenes = screenplay_data.get("screenplay", [])
    if not screenplay_scenes:
        logger.error("❌ Не найдены сцены в screenplay.json")
        return {"items": [], "consistency_rules": [], "error": "No scenes found in screenplay"}
    
    # Optional filtering by explicit scene_numbers
    if scene_numbers is not None:
        try:
            allow = {int(x) for x in (scene_numbers or [])}
        except Exception:
            allow = set()
        screenplay_scenes = [sc for sc in (screenplay_scenes or []) if int(sc.get("scene_number", 0)) in allow]

    # Optional limiting by max_scenes (after filtering)
    if max_scenes is not None:
        try:
            m = max(0, int(max_scenes))
        except Exception:
            m = 0
        screenplay_scenes = list(screenplay_scenes)[:m]

    logger.info(f"🎬 Обрабатываем {len(screenplay_scenes)} сцен из режиссерского сценария...")
    
    # Создаем директорию для shots
    shots_dir = f"plots/storybooks/{project_id}/97_shots"
    os.makedirs(shots_dir, exist_ok=True)
    
    shots_path = f"{shots_dir}/shots.json"
    fcpxml_path = f"{shots_dir}/shots_timeline.fcpxml"
    photo_fcpxml_path = f"{shots_dir}/photo_shots_timeline.fcpxml"
    
    # Проверяем существование файлов
    shots_exists = os.path.exists(shots_path)
    fcpxml_exists = os.path.exists(fcpxml_path)
    photo_fcpxml_exists = os.path.exists(photo_fcpxml_path)
    
    # If the caller requests a partial scope or force regen, do NOT short-circuit on existing shots.json.
    requested_partial = (scene_numbers is not None) or (max_scenes is not None)

    if shots_exists and fcpxml_exists and (not force) and (not requested_partial):
        logger.info("✅ Файлы shots.json и shots_timeline.fcpxml уже существуют, загружаем shots.json")
        with open(shots_path, "r", encoding="utf-8") as f:
            shots_data = json.load(f)
        
        # Генерируем photo FCPXML если его нет
        if not photo_fcpxml_exists:
            shots_items = shots_data.get("items", [])
            _generate_photo_fcpxml(project_id, shots_items, photo_fcpxml_path)
            logger.info(f"✅ Photo FCPXML для DaVinci Resolve сохранен: {photo_fcpxml_path}")
        
        return shots_data
    
    if shots_exists and not fcpxml_exists and (not force) and (not requested_partial):
        logger.info("📋 shots.json существует, но shots_timeline.fcpxml отсутствует - генерируем FCPXML файлы")
        with open(shots_path, "r", encoding="utf-8") as f:
            shots_data = json.load(f)
        
        # Генерируем FCPXML для видео
        shots_items = shots_data.get("items", [])
        _generate_fcpxml(project_id, shots_items, fcpxml_path)
        logger.info(f"✅ FCPXML для DaVinci Resolve сохранен: {fcpxml_path}")
        
        # Генерируем photo FCPXML если его нет
        if not photo_fcpxml_exists:
            _generate_photo_fcpxml(project_id, shots_items, photo_fcpxml_path)
            logger.info(f"✅ Photo FCPXML для DaVinci Resolve сохранен: {photo_fcpxml_path}")
        
        return shots_data
    
    # Генерируем shots.json (и FCPXML, если его нет)
    logger.info("🎬 Генерируем shots.json...")

    # Читаем seed из brief.json
    brief_path = f"plots/storybooks/{project_id}/00_brief.json"
    seed = random.randint(1, 1000000)  # Значение по умолчанию
    if os.path.exists(brief_path):
        try:
            with open(brief_path, "r", encoding="utf-8") as f:
                brief_data = json.load(f)
            seed = brief_data.get("seed", seed)
            logger.info(f"🎲 Используем seed из brief.json: {seed}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось прочитать seed из brief.json: {e}, используем случайный seed: {seed}")
    else:
        logger.warning(f"⚠️ brief.json не найден, используем случайный seed: {seed}")
    
    all_start_items = []  # Все start кадры
    all_end_items = []    # Все end кадры
    checkpoint_state_lock = threading.Lock()
    checkpoint_scene_items: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}
    total_shots = 0
    item_number = 1
    page_number = 1  # Последовательный счетчик для page_number

    def _checkpoint_partial_progress(
        *,
        scene_number: int,
        shot_number: int,
        scene_start_items: List[Dict[str, Any]],
        scene_end_items: List[Dict[str, Any]],
    ) -> None:
        with checkpoint_state_lock:
            checkpoint_scene_items[int(scene_number)] = {
                "start": [dict(item) for item in scene_start_items],
                "end": [dict(item) for item in scene_end_items],
            }
            checkpoint_data = _build_shots_data(
                all_start_items=[
                    item
                    for scene_data in checkpoint_scene_items.values()
                    for item in scene_data.get("start", [])
                ],
                all_end_items=[
                    item
                    for scene_data in checkpoint_scene_items.values()
                    for item in scene_data.get("end", [])
                ],
                consistency_rules=consistency_rules,
                seed=seed,
                session_id=session_id,
                scene_numbers=scene_numbers,
                max_scenes=max_scenes,
            )
            _write_shots_data(shots_path, checkpoint_data)
            logger.info(
                "💾 Checkpoint shots.json обновлен после shot %s сцены %s (%s items)",
                shot_number,
                scene_number,
                len(checkpoint_data.get("items", [])),
            )
    
    def _process_scene_worker(scene: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        local_start: List[Dict[str, Any]] = []
        local_end: List[Dict[str, Any]] = []
        previous_shot_has_end_shot = False
        previous_end_llm_result = None
        previous_end_shot_item = None

        scene_number = scene.get("scene_number", 1)
        scene_action = scene.get("action", "")
        scene_characters = scene.get("characters", [])
        storyboard = scene.get("storyboard", [])
        raw_scene_continuity_facts = scene.get("scene_continuity_facts")
        scene_continuity_facts = (
            dict(raw_scene_continuity_facts)
            if isinstance(raw_scene_continuity_facts, dict)
            else {}
        )
        if scene_continuity_facts:
            logger.info(
                "📎 [scene %s] Используем scene_continuity_facts из входных данных",
                scene_number,
            )
        else:
            logger.info(
                "ℹ️ [scene %s] scene_continuity_facts отсутствуют во входе; продолжаем без отдельного continuity-pass",
                scene_number,
            )
        
        logger.info(f"📋 [scene {scene_number}] Старт обработки: {len(storyboard)} кадрами")

        local_item = 1
        local_page = 1
        
        for shot in storyboard:
            shot_number = shot.get("shot_number", 1)
            shot_description = shot.get("description", "")
            camera_plan = shot.get("camera_plan", "")
            timing = shot.get("timing", "")
            
            logger.info(f"🎬 [scene {scene_number}] Начинаем генерацию shot {shot_number}: {camera_plan} - {timing}")
            
            start_llm_result = None
            start_shot_item = None
            end_shot_item = None
            try:
                extended_context = _build_extended_context(
                    project_id=project_id,
                    scene=scene,
                    storyboard=storyboard,
                    shot_number=shot_number,
                    scene_action=scene_action,
                    shot_description=shot_description,
                    camera_plan=camera_plan,
                    scene_characters=scene_characters,
                    screenplay_data=screenplay_data,
                    characters_data=characters_data,
                    locations_data=locations_data,
                    scene_continuity_facts=scene_continuity_facts,
                    style_images=style_images_data,
                )
            except Exception as e:
                logger.error(f"❌ [scene {scene_number}] Ошибка построения extended_context для shot {shot_number}: {e}")
                continue
            
            if previous_shot_has_end_shot and previous_end_shot_item and previous_end_llm_result:
                try:
                    logger.info(f"🔗 [scene {scene_number}] Генерируем START кадр {shot_number} на основе предыдущего END")
                    shot_item = _convert_end_to_start_shot_item(
                        previous_end_shot_item=previous_end_shot_item,
                        previous_end_llm_result=previous_end_llm_result,
                        project_id=project_id,
                        scene_number=scene_number,
                        shot_number=shot_number,
                        page_number=local_page,
                        item_number=local_item,
                        camera_plan=camera_plan,
                        timing=timing,
                        characters_data=characters_data,
                        locations_data=locations_data,
                        scene_action=scene_action,
                        shot_description=shot_description,
                        scene_characters=scene_characters,
                        scene_continuity_facts=scene_continuity_facts,
                        location_time=scene.get("location_time", ""),
                        language=language,
                        seed=seed
                    )
                    start_shot_item = shot_item
                    start_shot_item["video_prompt"] = ""
                    start_llm_result = dict(previous_end_llm_result or {})
                    start_llm_result["video_prompt"] = ""
                    local_start.append(shot_item)
                    local_item += 1
                    local_page += 1
                    logger.info(f"✅ [scene {scene_number}] Успешно сгенерирован связанный START кадр {shot_number}")
                except Exception as e:
                    logger.error(f"❌ [scene {scene_number}] Ошибка конвертации end→start: {e}")
                    start_llm_result = None
            
            if not start_llm_result:
                try:
                    logger.info(f"🆕 [scene {scene_number}] Генерируем независимый START кадр {shot_number}")
                    start_llm_result = _generate_shot_prompt(
                        extended_context=extended_context,
                        shot_type="start",
                        language=language,
                    )
                    if start_llm_result:
                        shot_item = _create_shot_item(
                            project_id=project_id,
                            scene_number=scene_number,
                            shot_number=shot_number,
                            shot_type="start",
                            page_number=local_page,
                            item_number=local_item,
                            camera_plan=camera_plan,
                            timing=timing,
                            llm_result=start_llm_result,
                            characters_data=characters_data,
                            locations_data=locations_data,
                            location_time=extended_context.get("location_time", ""),
                            location_canon_name=extended_context.get("location_canon_name", ""),
                            scene_action=scene_action,
                            shot_description=shot_description,
                            shot_frame_spec=extended_context.get("full_shot_frame_spec") or extended_context.get("shot_frame_spec"),
                            shot_frame_spec_cache_key=extended_context.get("shot_frame_spec_cache_key", ""),
                            scene_continuity_facts=extended_context.get("scene_continuity_facts"),
                            language=language,
                            is_linked_start=False,
                            seed=seed,
                            visual_style=str(extended_context.get("visual_style") or ""),
                            style_do_not_include=extended_context.get("style_do_not_include"),
                        )
                        start_shot_item = shot_item
                        start_shot_item["video_prompt"] = ""
                        start_llm_result["video_prompt"] = ""
                        local_start.append(shot_item)
                        local_item += 1
                        local_page += 1
                        logger.info(f"✅ [scene {scene_number}] Успешно сгенерирован START кадр {shot_number}")
                except Exception as e:
                    logger.error(f"❌ [scene {scene_number}] Ошибка генерации START: {e}")
                    raise RuntimeError(
                        f"[scene {scene_number}] Ошибка генерации START для shot {shot_number}: {e}"
                    ) from e
            
            if generate_end_shots and start_llm_result and start_llm_result.get("add_end_shot", "true").lower() == "true":
                try:
                    logger.info(f"🎯 [scene {scene_number}] Генерируем END кадр {shot_number}")
                    end_llm_result = _generate_shot_prompt(
                        extended_context=extended_context,
                        shot_type="end",
                        video_prompt="",
                        start_llm_result=start_llm_result,
                        language=language,
                    )
                    if end_llm_result:
                        end_llm_result["video_prompt"] = ""
                        shot_frame_spec_for_item = (
                            extended_context.get("full_shot_frame_spec")
                            or extended_context.get("shot_frame_spec")
                        )
                        start_location_name = ""
                        if isinstance(start_shot_item, dict):
                            start_locations = start_shot_item.get("locations") or []
                            if start_locations and isinstance(start_locations[0], dict):
                                start_location_name = str(start_locations[0].get("name") or "").strip()

                        requested_end_location_name = (
                            str(extended_context.get("location_canon_name") or "").strip()
                            or str(end_llm_result.get("location") or "").strip()
                        )
                        end_location_canon_name = requested_end_location_name
                        if start_location_name:
                            allow_location_change = _allow_end_location_change_via_llm(
                                start_location_name=start_location_name,
                                candidate_end_location_name=requested_end_location_name,
                                shot_description=shot_description,
                                scene_action=scene_action,
                                shot_frame_spec=shot_frame_spec_for_item,
                            )
                            if not allow_location_change:
                                end_location_canon_name = start_location_name
                                logger.info(
                                    "🔒 END LOCATION LOCK: scene=%s shot=%s start='%s' requested_end='%s' -> keep start location",
                                    scene_number,
                                    shot_number,
                                    start_location_name,
                                    requested_end_location_name,
                                )
                            else:
                                logger.info(
                                    "🔁 END LOCATION CHANGE ALLOWED: scene=%s shot=%s start='%s' -> end='%s'",
                                    scene_number,
                                    shot_number,
                                    start_location_name,
                                    requested_end_location_name,
                                )
                        end_shot_item = _create_shot_item(
                            project_id=project_id,
                            scene_number=scene_number,
                            shot_number=shot_number,
                            shot_type="end",
                            page_number=local_page,
                            item_number=local_item,
                            camera_plan=camera_plan,
                            timing=timing,
                            llm_result=end_llm_result,
                            characters_data=characters_data,
                            locations_data=locations_data,
                            location_time=extended_context.get("location_time", ""),
                            location_canon_name=end_location_canon_name,
                            scene_action=scene_action,
                            shot_description=shot_description,
                            shot_frame_spec=shot_frame_spec_for_item,
                            shot_frame_spec_cache_key=extended_context.get("shot_frame_spec_cache_key", ""),
                            scene_continuity_facts=extended_context.get("scene_continuity_facts"),
                            language=language,
                            is_linked_start=False,
                            seed=seed,
                            visual_style=str(extended_context.get("visual_style") or ""),
                            style_do_not_include=extended_context.get("style_do_not_include"),
                        )
                        end_shot_item["video_prompt"] = ""
                        local_end.append(end_shot_item)
                        local_item += 1
                        local_page += 1
                        # Прогрессивный checkpoint должен materialize-иться уже после готовых
                        # START/END shot items, даже если final transition video_prompt зависнет или упадет позже.
                        _checkpoint_partial_progress(
                            scene_number=scene_number,
                            shot_number=shot_number,
                            scene_start_items=local_start,
                            scene_end_items=local_end,
                        )
                        final_video_prompt = _generate_transition_video_prompt(
                            start_llm_result=start_llm_result,
                            end_llm_result=end_llm_result,
                            extended_context=extended_context,
                        )
                        if not final_video_prompt:
                            logger.error(f"❌ [scene {scene_number}] Не удалось сгенерировать финальный video_prompt для shot {shot_number}")
                            raise RuntimeError(
                                f"[scene {scene_number}] Не удалось сгенерировать финальный video_prompt для shot {shot_number}"
                            )
                        start_llm_result["video_prompt"] = final_video_prompt
                        if start_shot_item is not None:
                            start_shot_item["video_prompt"] = final_video_prompt
                        logger.info(f"✅ [scene {scene_number}] Успешно сгенерирован END кадр {shot_number}")
                        can_link = str(end_llm_result.get("should_link_as_next_start", "false")).strip().lower() == "true"
                        can_use_as_reference = str(end_llm_result.get("should_use_prev_end_as_reference", "false")).strip().lower() == "true"
                        previous_shot_has_end_shot = can_link or can_use_as_reference
                        if can_link or can_use_as_reference:
                            previous_end_llm_result = end_llm_result
                            previous_end_shot_item = end_shot_item
                        else:
                            previous_end_llm_result = None
                            previous_end_shot_item = None
                    else:
                        previous_shot_has_end_shot = False
                        previous_end_llm_result = None
                        previous_end_shot_item = None
                except Exception as e:
                    logger.error(f"❌ [scene {scene_number}] Ошибка генерации END: {e}")
                    previous_shot_has_end_shot = False
                    previous_end_llm_result = None
                    previous_end_shot_item = None
                    raise RuntimeError(
                        f"[scene {scene_number}] Ошибка генерации END для shot {shot_number}: {e}"
                    ) from e
            else:
                if start_llm_result is not None:
                    start_llm_result["video_prompt"] = ""
                if start_shot_item is not None:
                    start_shot_item["video_prompt"] = ""
                previous_shot_has_end_shot = False
                previous_end_llm_result = None
                previous_end_shot_item = None

            _checkpoint_partial_progress(
                scene_number=scene_number,
                shot_number=shot_number,
                scene_start_items=local_start,
                scene_end_items=local_end,
            )
            
        return {"start": local_start, "end": local_end}

    # Параллельная обработка сцен
    max_workers = min(5, len(screenplay_scenes)) if screenplay_scenes else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_scene_worker, scene): scene for scene in screenplay_scenes}
        for fut in as_completed(futures):
            try:
                res = fut.result()
                all_start_items.extend(res.get("start", []))
                all_end_items.extend(res.get("end", []))
                checkpoint_data = _build_shots_data(
                    all_start_items=all_start_items,
                    all_end_items=all_end_items,
                    consistency_rules=consistency_rules,
                    seed=seed,
                    session_id=session_id,
                    scene_numbers=scene_numbers,
                    max_scenes=max_scenes,
                )
                _write_shots_data(shots_path, checkpoint_data)
                logger.info(
                    "💾 Checkpoint shots.json синхронизирован после завершения сцены %s (%s items)",
                    futures[fut].get("scene_number"),
                    len(checkpoint_data.get("items", [])),
                )
            except Exception as e:
                s = futures[fut]
                logger.error(f"❌ Ошибка обработки сцены {s.get('scene_number')}: {e}")
                raise RuntimeError(
                    f"Ошибка обработки сцены {s.get('scene_number')}: {e}"
                ) from e

    shots_data = _build_shots_data(
        all_start_items=all_start_items,
        all_end_items=all_end_items,
        consistency_rules=consistency_rules,
        seed=seed,
        session_id=session_id,
        scene_numbers=scene_numbers,
        max_scenes=max_scenes,
    )
    shots_items = shots_data["items"]
    
    logger.info("🈯 Shots prompts already generated on language: %s", language)
    
    # Сохраняем shots.json
    _write_shots_data(shots_path, shots_data)
    
    logger.info(f"✅ Кадры сценария сохранены: {shots_path}")
    
    # Генерируем FCPXML для DaVinci Resolve только если его не было
    if not fcpxml_exists:
        _generate_fcpxml(project_id, shots_items, fcpxml_path)
        logger.info(f"✅ FCPXML для DaVinci Resolve сохранен: {fcpxml_path}")
    else:
        logger.info(f"ℹ️ FCPXML уже существует: {fcpxml_path}")

    # Генерируем photo FCPXML для DaVinci Resolve только если его не было
    if not photo_fcpxml_exists:
        _generate_photo_fcpxml(project_id, shots_items, photo_fcpxml_path)
        logger.info(f"✅ Photo FCPXML для DaVinci Resolve сохранен: {photo_fcpxml_path}")
    else:
        logger.info(f"ℹ️ Photo FCPXML уже существует: {photo_fcpxml_path}")

    # Возвращаем данные в формате, совместимом с artist_agent_batch_edit_tool
    logger.info(f"📊 Возвращаем данные shots для дальнейшей обработки: {total_shots} кадров")
    return shots_data


def _build_shots_data(
    *,
    all_start_items: List[Dict[str, Any]],
    all_end_items: List[Dict[str, Any]],
    consistency_rules: List[Dict[str, Any]],
    seed: int,
    session_id: str,
    scene_numbers: Optional[List[int]],
    max_scenes: Optional[int],
) -> Dict[str, Any]:
    shots_items: List[Dict[str, Any]] = []
    shots_by_key: Dict[tuple[int, int], Dict[str, Dict[str, Any]]] = {}

    for start_item in all_start_items:
        scene_num = start_item.get("scene_number", 1)
        shot_num = start_item.get("shot_number", 1)
        key = (scene_num, shot_num)
        if key not in shots_by_key:
            shots_by_key[key] = {}
        shots_by_key[key]["start"] = start_item

    for end_item in all_end_items:
        scene_num = end_item.get("scene_number", 1)
        shot_num = end_item.get("shot_number", 1)
        key = (scene_num, shot_num)
        if key not in shots_by_key:
            shots_by_key[key] = {}
        shots_by_key[key]["end"] = end_item

    for key in sorted(shots_by_key.keys()):
        shot_data = shots_by_key[key]
        if "start" in shot_data:
            shots_items.append(shot_data["start"])
        if "end" in shot_data:
            shots_items.append(shot_data["end"])

    for idx, item in enumerate(shots_items, start=1):
        item["page_number"] = idx
        item["number"] = idx

    return {
        "items": shots_items,
        "consistency_rules": consistency_rules,
        "parallel_generation": True,
        "seed": seed,
        "generated_session_id": session_id,
        "generated_scope": (
            ("scenes:" + ",".join(str(int(x)) for x in scene_numbers)) if scene_numbers is not None
            else (f"partial:{int(max_scenes)}" if max_scenes is not None else "all")
        ),
    }


_SHOTS_WRITE_LOCK = threading.Lock()


def _write_shots_data(shots_path: str, shots_data: Dict[str, Any]) -> None:
    """Атомарная потокобезопасная запись shots.json.

    Checkpoint'ы пишутся параллельно из нескольких worker-потоков в
    ThreadPoolExecutor; без синхронизации получался повреждённый JSON.
    """
    directory = os.path.dirname(shots_path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{shots_path}.tmp"
    with _SHOTS_WRITE_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(shots_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, shots_path)

def _classify_shot_type(shot_description: str, camera_plan: str) -> Dict[str, Any]:
    """
    Детерминистически определяет тип шота и возвращает structured edge-case rules.
    Вызывается ДО LLM-запроса, чтобы передать LLM только релевантные правила.
    """
    desc_lower = (shot_description or "").lower()
    cam_lower = (camera_plan or "").lower()
    overrides = {"shot_type": "standard", "edge_case_rules": []}

    # THROW / HURL / RELEASE
    throw_verbs = ["вышвыривает", "выбрасывает", "бросает", "швыряет", "throws", "hurls", "tosses"]
    if any(v in desc_lower for v in throw_verbs):
        overrides["shot_type"] = "throw_release"
        overrides["edge_case_rules"].append({
            "rule": "THROW/RELEASE",
            "start_t0": "object still in hand/claw, static pre-release posture, NOT in mid-air",
            "end_tfinal": "object NOT in hand, default OUT OF FRAME (unless storyboard requires visible flight)",
            "count_lock": "single object only, no duplicates, other hand empty",
        })

    # FALLING PROP → hand / body part
    fall_cues = ["падает в руку", "опускается на руку", "falls into", "lands in his hand",
                 "падает на рог", "падает на голову", "падает на плечо", "falls onto"]
    if any(c in desc_lower for c in fall_cues):
        overrides["shot_type"] = "falling_prop"
        overrides["edge_case_rules"].append({
            "rule": "FALLING_PROP_LANDING",
            "start_t0": "prop in mid-air, visible gap, NO contact",
            "end_tfinal": "prop resting on target body part (contact/received), NOT hovering",
        })

    # CATCH / HANG BY THREAD
    catch_cues = ["цепляется", "висит на одной нитке", "свисает", "catches on the edge", "hangs by"]
    if any(c in desc_lower for c in catch_cues):
        overrides["shot_type"] = "catch_hang"
        overrides["edge_case_rules"].append({
            "rule": "CATCH/HANG_BY_THREAD",
            "start_t0": "early fall, prop entering frame, NO thread/attachment yet",
            "end_tfinal": "post-catch, prop attached by single thread, attachment point visible",
            "forbidden": "floating/hovering/levitating instead of gravity+attachment",
        })

    # PROP HERO (object-in-flight)
    flight_cues = ["летит", "flies", "arc", "slow-motion", "в воздухе"]
    if any(c in desc_lower for c in flight_cues) and overrides["shot_type"] == "standard":
        overrides["shot_type"] = "prop_hero"
        overrides["edge_case_rules"].append({
            "rule": "PROP_HERO_SHOT",
            "primary_focus": "flying object",
            "background": "blurred, generic, only from shot_description",
            "causality_anchors": "source cue + trajectory + optional blurred target",
            "forbidden": "invented objects not in shot_description",
        })

    # ENVIRONMENT HERO (зал/толпа/пауза)
    env_cues = ["зал", "толпа", "тишин", "замер", "пауза", "crowd", "silence", "room"]
    if any(c in desc_lower for c in env_cues) and "персонаж" not in desc_lower:
        if overrides["shot_type"] == "standard":
            overrides["shot_type"] = "environment_hero"
            overrides["edge_case_rules"].append({
                "rule": "ENVIRONMENT_HERO",
                "primary_focus": "space and mass reaction (or absence)",
                "characters": "secondary only (silhouettes/small figures for scale)",
                "forbidden": "specific character as main subject unless in shot_description",
            })

    # REACTION SHOT
    if "reaction" in cam_lower or "реакц" in cam_lower:
        overrides["shot_type"] = "reaction"
        overrides["edge_case_rules"].append({
            "rule": "REACTION_SHOT",
            "primary_focus": "reacting character",
            "gaze": "off-screen/toward event, NOT at camera",
        })

    # POV
    if "pov" in cam_lower or "point of view" in cam_lower or "субъективн" in cam_lower:
        overrides["shot_type"] = "pov"
        overrides["edge_case_rules"].append({
            "rule": "POV_SHOT",
            "primary_focus": "what the character sees",
            "character_ref": "keep POV character reference even if not visible",
            "body_parts": "if visible, MUST match character reference (species/anatomy)",
        })

    # MATCH CUT RECIPIENT
    match_cues = ["получает", "receives", "принимает", "match cut"]
    if any(c in desc_lower for c in match_cues):
        overrides["edge_case_rules"].append({
            "rule": "MATCH_CUT_RECIPIENT",
            "instigator": "OFF-SCREEN only, do not add as visible character",
        })

    # REFLECTION
    refl_cues = ["отражен", "зеркал", "reflection", "mirror"]
    if any(c in desc_lower for c in refl_cues):
        overrides["edge_case_rules"].append({
            "rule": "REFLECTION_LOCK",
            "principle": "derive from camera geometry + subject orientation + mirror position",
            "forbidden": "symbolic/convenient angle; anthropomorphized reflections on food/objects",
        })

    return overrides


def _generate_shot_artistic(
    technical_params: Dict[str, Any],
    extended_context: Dict[str, Any],
    language: str = "en",
) -> Optional[Dict[str, Any]]:
    """
    Второй этап: художественная генерация промптов.
    Создает художественные описания на основе технических параметров.
    """
    
    prompt_language_label = _get_prompt_language_label(language)
    edit_prefix = _get_prompt_edit_prefix(language)
    create_prefix = _get_prompt_create_prefix(language)

    system_prompt = (
        _build_state_to_image_common_core(
            phase_name="START",
            state_spec_name="start_state_spec",
            composition_field="spatial_composition",
            camera_position_field="camera_position",
            orientation_field="character_orientation",
            summary_field="initial_state_summary",
        )
        + f"""
=== СЛОЙ A: РОЛЬ И КОНТРАКТ ===

Ты — креативный директор. Создай команды редактирования для START кадра (T=0) на основе технических параметров.

**LANGUAGE CONTRACT:**
- `english_prompt`, `negative_prompt`, `reference_roles_instruction`, `main_subject`, `initial_state_summary`, `spatial_composition` — строго на {prompt_language_label}.
- `video_prompt` — ТОЛЬКО на английском языке.
- Continuity-редактирование: префикс `{edit_prefix}`. Создание с нуля: префикс `{create_prefix}`.

**[P0 CRITICAL] SHOT_FRAME_SPEC = SOURCE OF TRUTH:**
- `shot_frame_spec` — авторитетная спецификация шота. Поля `primary_subject`, `visible_characters`, `must_show`, `must_not_show`, `visible_readable_texts`, `hidden_readable_texts`, `world_physics` обязательны.
- `facial_expression`, `gaze_direction`, `pose_signature` из `shot_frame_spec` (для START фазы это уже start_state_spec) обязательны и не могут быть сведены к нейтральной позе/лицу.
- Нельзя добавлять объекты/персонажей, которых нет в `must_show` и `shot_description`. Нельзя удалять обязательные факты из `must_show`.
- Все structured fields (`main_subject`, `initial_state_summary`, `spatial_composition`, `camera_position`, `character_orientation`, `point_of_view`) должны быть непустыми и согласованы с `shot_frame_spec`.

=== СЛОЙ B: ПОШАГОВЫЙ АЛГОРИТМ ГЕНЕРАЦИИ ===

Выполни по порядку:

**ШАГ 1 — SUBJECT:** main_subject = `shot_frame_spec.primary_subject`.

**ШАГ 2 — T=0 STATE (initial_state_summary):**
T=0 управляется полем `shot_frame_spec.t0_mode` (для START фазы spec уже фильтрован до start_state_spec). Если поле отсутствует — трактуй как `frozen`.

- `t0_mode == "frozen"` → T=0 = ИСХОДНАЯ ПОЗИЦИЯ ДО события:
  * Если shot_description описывает действие (grab/hit/shoot/eat/throw/transform): VISIBLE GAP между субъектом и целью, "positioned near [target]", "oriented toward [target]", motionless/frozen/pre-action posture. Не показывай результат действия в START.
  * Если shot_description описывает трансформацию среды (пол проваливается, дверь открывается, мост рушится): поверхность/объект полностью ЦЕЛОСТНЫЙ и СТАБИЛЬНЫЙ, T=0 = абсолютный покой, трансформация ещё не началась.
  * ЗАПРЕЩЁННЫЕ ФОРМУЛИРОВКИ для frozen: "на пороге", "готовящийся к", "первые признаки", "начинающиеся трещины", "нестабильность", "imminent", "about to", "threshold of", "on the verge". Если поверхность ещё цела — она просто ЦЕЛА, без foreshadowing.

- `t0_mode == "early_motion"` → T=0 = САМОЕ НАЧАЛО читаемой фазы движения, без финального результата:
  * Допустимы первые видимые признаки фазы из `shot_frame_spec.world_physics` / `must_show`: оторванная от опоры стопа, корпус уже в начальном наклоне, рука уже начала жест, опора уже начала деформироваться. НЕ описывай завершившийся результат события.
  * Не используй формулировки полного покоя ("motionless", "frozen", "pre-action") — они противоречат early_motion. Используй формулировки начальной фазы ("just initiated", "early phase of [action]", "motion just begun").
  * `must_show` обязан подтверждать выбранную начальную фазу — не показывай статическую позу, если spec фиксирует уже оторванную ногу или уже начавшийся жест.

- `t0_mode == "mid_action"` → T=0 = СЕРЕДИНА уже идущего события, кадр внутри процесса:
  * Тело/опора/реквизит уже в промежуточном состоянии (середина прыжка, середина падения, середина удара, корпус уже в воздухе) — без финального результата END.
  * Не используй формулировки полного покоя и формулировки исходной позиции ("about to", "positioned to", "ready to") — они противоречат mid_action.
  * Удерживай `world_physics`/`pose_signature` из текущего `shot_frame_spec` как промежуточную фазу, без сваливания в финальный кадр END.

- Если shot_description описывает чистую статику → t0_mode скорее всего `frozen`; T=0 = то, что видно.
- "Static" ≠ "neutral": сохраняй выразительный gesture signature независимо от t0_mode.

**ШАГ 3 — FACE/GAZE:**
- facial_expression / gaze_direction из spec → обязательны (не default'и к neutral/blank/facing_camera)
- Если spec пуст, но лицо читается → выведи из shot_description + world_physics (микро-реакция, не пустота)
- world_physics описывает физику кадра: опора, контакты, зазоры, перекрытия → следуй ему
- world_physics.forbidden_implications → не допускай в english_prompt

**ШАГ 4 — CONTINUITY DECISION:**
- Смена локации / смена главного персонажа / параллельный монтаж / смена крупности на 2+ ступени → "Create"
- Локация И персонаж совпадают И крупность близка (±1 ступень) → "Edit image 1"
- Порядок референсов: image 1 = continuity (если есть), image 2+ = character refs, image N = location ref

**ШАГ 5 — ENGLISH_PROMPT (собери по шаблону):**
[Editing instruction] [Shot Size]: [Subject + pose] [Position left/center/right + fore/mid/back] [Location context as unified objects] [Lighting + direction] [Camera angle] [Lens specs]
- location_context.key_features → описывай КАК ЕДИНОЕ ЦЕЛОЕ ("wall with embedded neon symbol", не два отдельных объекта)
- Close-up: освещение БЕЗ температуры K. Medium/Wide: с температурой K.
- Lens: Close-up → 85mm f/1.8. Medium → 50mm f/2.0. Wide → 24mm f/8.0.
- Ratio ≈1.0 (0.98–1.02) → НЕ генерируй команду масштабирования.
- scene_continuity_facts: добавляй гардероб/предметы только если видимы при данной крупности. Close-up → минимальные подсказки.

**ШАГ 6 — REFERENCE ROLES:**
- `reference_roles_instruction`: "image N: [роль] ([аспекты: лицо/одежда/стиль для персонажа; композиция/палитра для локации])".
- Не общие фразы "image 1 as character". Укажи конкретный аспект.

**ШАГ 7 — VIDEO_PROMPT (English-only, одна строка):**
Формат: Camera (с позицией) → Subject (quality keyword + action, до 2 шагов) → Environment (микродинамика) → Tempo
- video_prompt = tight English paraphrase shot_description + camera clause. Ничего лишнего.
- Направление движения: "спускается" → "descends" (НЕ "ascends"). "Касается" → "makes contact" (НЕ "approaching").
- Если взаимодействие объект-персонаж → включи видимые части персонажа ("nape/upper back visible in frame").
- Close-up: обязателен токен "close-up". Фон = blur-hint, без room-establishing.
- Split screen: обязателен токен "split-screen".
- Quality keywords: natural/energetic/slow and deliberate/graceful/confident/fluid movement.

**ШАГ 8 — NEGATIVE_PROMPT (КОМПАКТНО, максимум 30 слов):**
- Базовый: blurry, motion blur, text, watermark
- Close-up: + "no extra/double/missing [body parts], no deformed anatomy"
- Wide: + "no cropping, no truncation, no cut-off subjects"
- POV: + "no third-person view, no external camera angle"
- НЕ РАЗДУВАЙ negative повторами одного запрета в разных формулировках. Одна идея = одна фраза.

**ШАГ 9 — SELF-CHECK (обязательно перед возвратом JSON):**
1. Каждый noun phrase в english_prompt поддержан shot_description или shot_frame_spec? Если нет → удали.
2. english_prompt, initial_state_summary, spatial_composition описывают ОДИН кадр? Если расходятся → согласуй.
3. Процессные глаголы в english_prompt согласованы с `shot_frame_spec.t0_mode`? Для `frozen` (или если поле отсутствует) — замени walking/running/about to на статичные (positioned, frozen). Для `early_motion`/`mid_action` — наоборот, не "статизируй" уже идущую фазу до полного покоя.
4. camera_position и character_orientation совместимы? (behind + facing_camera = конфликт → исправь)
5. Согласованность с `t0_mode`: для `frozen` при действии показывай pre-action posture с visible gap; для `early_motion` — самые ранние видимые признаки фазы; для `mid_action` — промежуточное состояние без финального результата.
6. video_prompt содержит noun phrases, которых нет в shot_description? → удали.
7. Персонажи смотрят друг на друга (если взаимодействие)? → не "looking at camera".

=== СЛОЙ C: СПРАВОЧНЫЕ ТАБЛИЦЫ ===

**СПЕЦИАЛЬНЫЕ ТИПЫ КАДРОВ:**
| Тип | Primary focus | Правила |
|-----|---------------|---------|
| REACTION SHOT | реагирующий персонаж | фокус на реакции, не "looking at camera" |
| POV | то, что видит персонаж | сохраняй character reference для видимых частей тела POV-персонажа |
| WIDE | вся сцена | "full body, do not crop edges" |
| CLOSE-UP частей тела | [часть] + partial [face/body] | "preserve anatomical identity" |
| PROP HERO (object-in-flight) | летящий объект | минимальные causality anchors (source + trajectory), фон размытый |
| ENVIRONMENT HERO (зал/толпа) | пространство | персонажи вторичны (силуэты), не тащи героя из предыдущего кадра |
| Объект-персонаж взаимодействие | объект взаимодействия | включи видимые части персонажа в кадр |

**ЗАПРЕЩЕННЫЕ ФОРМУЛИРОВКИ (english_prompt START):**
- Всегда запрещено (любой t0_mode): then / after that / next / 1.0x / 1x / 0.99x.
- Если `shot_frame_spec.t0_mode == "frozen"` (или поле отсутствует): дополнительно запрещены will / about to / going to / begins to / starts to / walking / running / moving / approaching — это все процессные глаголы, несовместимые с frozen T=0.
- Если `t0_mode == "early_motion"`: разрешены формулировки начальной фазы (walking just begun / first step / early phase of running / motion just initiated); запрещены формулировки полного покоя (motionless / frozen / pre-action posture) и формулировки финального результата.
- Если `t0_mode == "mid_action"`: разрешены формулировки середины процесса (mid-stride / mid-leap / mid-fall / in motion); запрещены формулировки полного покоя и формулировки исходной позиции (about to / positioned to / ready to).

**ФОРМАТ ВЫВОДА:**
{{
  "main_subject": "shot_frame_spec.primary_subject",
  "camera_position": "из technical params",
  "character_orientation": "из technical params",
  "spatial_composition": "непустая композиция",
  "point_of_view": "objective / pov / etc",
  "initial_state_summary": "статичное T=0 summary",
  "english_prompt": "команда редактирования START T=0",
  "negative_prompt": "запреты по типу кадра",
  "reference_image_paths": ["путь1", "путь2"],
  "reference_roles_instruction": "image 1 as...; image 2 as...",
  "characters": ["имя1", "имя2"]
}}

**КРИТИЧЕСКИ ВАЖНО - ФОРМАТ ОТВЕТА:**
Твой ответ должен быть ТОЛЬКО чистым валидным JSON без каких-либо markdown-блоков, тегов ```json или дополнительного текста.
"""
    )

    # Извлекаем вспомогательные переменные
    continuity_ref_path = extended_context.get('continuity_reference_path', '')
    has_continuity = extended_context.get('has_continuity_reference', False)
    location_ctx = technical_params.get('location_context', {})
    shot_size = technical_params.get('shot_size', '')
    
    # Подготавливаем компактные структуры для LLM (уменьшаем размер контекста)
    start_summary = {
        "characters": technical_params.get("characters", []),
        "main_subject": technical_params.get("main_subject", ""),
        "camera_position": technical_params.get("camera_position", ""),
        "character_orientation": technical_params.get("character_orientation", ""),
        "spatial_composition": technical_params.get("spatial_composition", ""),
        "point_of_view": technical_params.get("point_of_view", "objective"),
        "initial_state_summary": technical_params.get("initial_state_summary", ""),
        "prop_continuity": technical_params.get("prop_continuity", {}),
        "lighting_style": technical_params.get("lighting_style", ""),
        "camera_angle": technical_params.get("camera_angle", ""),
    }
    
    context_for_decisions = {
        "scene_action": extended_context.get("scene_action", ""),
        "shot_description": extended_context.get("shot_description", ""),
        "camera_plan": extended_context.get("camera_plan", ""),
        "scene_pacing": extended_context.get("scene_pacing", ""),
        "narrative_position": extended_context.get("narrative_position", ""),
        "scene_mood": extended_context.get("scene_mood", ""),
        "location_time": extended_context.get("location_time", ""),
        "lighting_context": extended_context.get("lighting_context", ""),
        "scene_sound": extended_context.get("scene_sound", ""),
        "visual_style": extended_context.get("visual_style", ""),
        "previous_shot": extended_context.get("previous_shot", {}),
        "next_shot": extended_context.get("next_shot", {}),
    }
    
    # Описания персонажей для визуализации
    shot_character_visual_profiles = (
        extended_context.get('start_shot_character_visual_profiles')
        or extended_context.get('shot_character_visual_profiles')
        or extended_context.get('character_visual_profiles', [])
    )

    # Классифицируем тип шота и получаем ТОЛЬКО релевантные edge-case правила
    shot_type_info = _classify_shot_type(
        extended_context.get("shot_description", ""),
        extended_context.get("camera_plan", ""),
    )

    if has_continuity:
        start_location_mode_block = """
**[P0] РЕЖИМ START — ПРЕЕМСТВЕННОСТЬ (есть continuity reference с предыдущего кадра / START наследуется):**
- Главный источник уже видимой геометрии, реквизита и массовки — **image 1** (continuity). Не наполняй кадр **новыми** объектами из `location_context.key_objects` или описания локации, если их нет в image 1 и они не требуются явно в `shot_frame_spec.must_show` / `shot_description`.
- Паспорт локации используй для **согласования** палитры, материалов, характера света и атмосферы с уже существующим кадром; **не** как полный второй чеклист интерьера поверх image 1.
- При любом противоречии: **shot_frame_spec** и image 1 важнее, чем «богатый» список key_objects в паспорте локации.
"""
        start_key_objects_rule = (
            "key_objects из location_context — только если подтверждают то, что уже читается в image 1 или явно требуют shot_frame_spec; иначе не добавляй новые предметы по списку."
        )
    else:
        start_location_mode_block = """
**[P0] РЕЖИМ START — НЕЗАВИСИМЫЙ КАДР (нет continuity reference / кадр не наследуется от предыдущего END):**
- Устанавливай окружение по **паспорту локации** (`location_context`: description, lighting, atmosphere, color_palette, key_objects), референсу локации и `shot_frame_spec`.
- `shot_frame_spec` и `must_show` / `must_not_show` сильнее общего описания локации: не добавляй объекты, запрещённые спецификацией шота.
"""
        start_key_objects_rule = (
            "ОБЯЗАТЕЛЬНО опирайся на key_objects из location_context (в согласовании с shot_frame_spec), референс локации и must_show."
        )

    _bs_art = black_screen_storyboard_shot(
        str(extended_context.get("camera_plan") or ""),
        extended_context.get("full_shot_frame_spec") or extended_context.get("shot_frame_spec"),
    )
    black_screen_mode_block = ""
    if _bs_art:
        black_screen_mode_block = """
**[P0] BLACK SCREEN / ЧЁРНЫЙ ЭКРАН (приоритет над локацией и персонажами):**
- Визуально кадр = полное отсутствие изображения: следуй `shot_frame_spec.must_show` / `must_not_show` и `camera_plan`; не строй интерьер и не добавляй персонажей «по контексту сцены».
- `english_prompt` (START): одна панель ровного чёрного #000000; без силуэтов, без градиентов, без виньетки, без плёночного зерна, без текста/UI; не описывай локацию и не используй кино-оборудование (Arri Alexa и т.п.).
- `reference_image_paths`: [] (не подключай референсы локации/персонажа).
- `characters`: [].
- `negative_prompt`: усиль запреты на свет, силуэты, детали среды, фотореализм, «киношный» кадр; учти `style_do_not_include` из контекста, если передан.
"""

    user_prompt = f"""Создай художественные промпты на основе технических параметров с учетом полного контекста.
{black_screen_mode_block}
LANGUAGE CONTRACT:
- `english_prompt`, `negative_prompt`, `reference_roles_instruction`, `main_subject`, `initial_state_summary` и `spatial_composition` пиши строго на {prompt_language_label}.
- `video_prompt` оставляй строго на английском языке.
- Для continuity-редактирования используй `{edit_prefix}`.
- Для создания кадра с нуля используй `{create_prefix}`.

{_build_state_to_image_user_core(
    phase_name="START",
    state_spec_name="start_state_spec",
    summary_field="initial_state_summary",
    composition_field="spatial_composition",
    camera_position_field="camera_position",
    orientation_field="character_orientation",
    shot_size_field_name="shot_size",
)}

PER-SHOT CAST VISUAL CANON (ИСПОЛЬЗУЙ ТОЛЬКО ДЛЯ ВИЗУАЛИЗАЦИИ ЭТОГО КАДРА):
{json.dumps(shot_character_visual_profiles, ensure_ascii=False, indent=2)}

ТЕХНИЧЕСКИЕ ПАРАМЕТРЫ КАДРА:
{json.dumps(start_summary, ensure_ascii=False, indent=2)}

SHOT FRAME SPEC (авторитетный source of truth для шота):
{json.dumps(extended_context.get('shot_frame_spec', {}), ensure_ascii=False, indent=2)}

КРУПНОСТЬ КАДРА (ОБЯЗАТЕЛЬНО НАЧНИ ПРОМПТ С НЕЁ):
{shot_size}

ПАСПОРТ ЛОКАЦИИ (КРИТИЧНО ДЛЯ ФОНА):
<location_context>
{json.dumps(location_ctx, ensure_ascii=False, indent=2)}
</location_context>

{start_location_mode_block}

КОНТЕКСТ СЦЕНЫ И РЕШЕНИЙ:
{json.dumps(context_for_decisions, ensure_ascii=False, indent=2)}

scene_continuity_facts (устойчивые факты сцены из scene.action; применяй если уместно по camera_plan и не противоречит storyboard.description):
{json.dumps(extended_context.get('scene_continuity_facts', {}), ensure_ascii=False, indent=2)}

КОНТЕКСТ ПРЕЕМСТВЕННОСТИ:
Continuity reference path: {continuity_ref_path if continuity_ref_path else 'Нет'}
Есть continuity reference: {"Да" if has_continuity else "Нет"}

    ОБЯЗАТЕЛЬНО:
    - `shot_frame_spec` — это source of truth. Включи все факты из `must_show`, не добавляй того, чего нет в spec, и не делай главным субъектом никого, кроме `primary_subject`.
    - Если в `shot_frame_spec` или `start_state_spec` есть `world_physics`, переведи его в визуально читаемое состояние мира: опора, поверхность, устойчивость, контакты, зазоры, перекрытия. Не замещай это локальными edge-case описаниями.
    - **ВИЗУАЛИЗАЦИЯ**: Всегда добавляй к имени персонажа его прямые видимые черты из PER-SHOT CAST VISUAL CANON. Используй только лицо, глаза, пропорции, прическу, одежду, аксессуары и устойчивые видимые признаки. Не тащи в prompt роль, профессию, биографию, характер или речевые паттерны.
    - **MULTI-CHARACTER LOCK**: если в PER-SHOT CAST VISUAL CANON несколько персонажей, сохраняй их как отдельные визуальные сущности по `shot_role`; не смешивай одежду, лицо, аксессуары и силуэт между ними.
    - Если у персонажа в PER-SHOT CAST VISUAL CANON есть `phase_pose_signature`, используй её как authoritative позу именно этого персонажа в START.
    - **ЛИНЗЫ И КАМЕРА**: Включи технические параметры (85mm f/1.8 и т.д.) в english_prompt.
    - **НАЧНИ С ИНСТРУКЦИИ**: Если есть continuity reference → "{edit_prefix} {shot_size}:", иначе "{create_prefix} {shot_size}:"
    - **ACTION PRIORITY**: If `shot_description` contains an action (run, hit, shoot, eat), `video_prompt` MUST describe this movement. NEVER write 'subject remains still' for active shots.
- **ФОН ИЗ ПРАВИЛЬНОГО ИСТОЧНИКА**: 
  * Если continuity reference содержит четкий фон → "Keep {location_ctx.get('name', '')} from image 1"
  * Если continuity reference = крупный план → "Use {location_ctx.get('name', '')} from image [N] (location), position characters as in image 1"
- **BACKGROUND & EXTRAS LOCK (ОБЯЗАТЕЛЬНО при continuity reference)**:
  * Если ты редактируешь continuity reference (image 1) — фон и массовка должны оставаться теми же: НЕ меняй одежду/прически/лица background extras, НЕ добавляй/удаляй новые источники света и декор (люстры, лампы, бра, флаги, плакаты, растения), НЕ меняй архитектурные элементы, если этого нет в сценарии.
  * Референс локации используй только для согласования палитры/материалов/архитектуры, но НЕ как источник новых объектов, если они не присутствуют в image 1 и не требуются явно.
- Соблюдай ПРАВИЛО START-ФРЕЙМА — фиксируй T=0 (исходное состояние ПЕРЕД действием или МОМЕНТ процесса).
- Если действие подразумевает ВХОД персонажа в кадр — в T=0 этого персонажа в кадре НЕТ.
- КРИТИЧНО: Соблюдай ПРАВИЛА ПРОСТРАНСТВЕННОЙ КОМПОЗИЦИИ — используй camera_position и character_orientation для правильного описания ракурса.
- **КРУПНЫЕ ПЛАНЫ ЧАСТЕЙ ТЕЛА**: если spatial_composition описывает крупный план рук/ног, но упоминает частичное присутствие лица/тела — ВКЛЮЧИ обе части в промпт (например: "close-up of hands with partial face visible")
- **КРИТИЧНО - ОТРАЖЕНИЯ И ОТРАЖАЮЩИЕ ПОВЕРХНОСТИ:**
  * Если в кадре есть зеркало/отражение, выводи видимый результат из геометрии кадра: положения камеры, ориентации субъекта, положения отражающей поверхности и `world_physics`.
  * Отражение должно показывать то, что реально находится перед отражающей поверхностью, а не удобный "символический" ракурс.
  * Не хардкодь частные случаи; проверь, согласована ли отражённая часть тела/лица/спины с топологией текущего шота.
- **WORLD PHYSICS & GROUNDING (ОБЯЗАТЕЛЬНО):**
  * Если у шота есть `world_physics`, описывай физическое положение героя из него: на чём держится субъект, что происходит с поверхностью, есть ли контакт или зазор, что должно быть перекрыто.
  * Используй 1–2 anchor objects из `location_context.key_objects` только как подтверждение уже заданного физического состояния мира, а не как замену `world_physics`.
  * Если `world_physics` не задан, опирайся на минимально необходимую физическую заземлённость по смыслу shot_description, без локальных специальных случаев.
- **КРИТИЧНО - ПРЕДМЕТЫ ИНТЕРЬЕРА ИЗ РЕФЕРЕНСА ЛОКАЦИИ:**
  * Предметы интерьера из `location_context.key_objects` (стол, стул, комод, лампа, зеркало, окно и т.д.) ДОЛЖНЫ БРАТЬСЯ С РЕФЕРЕНСНОГО ИЗОБРАЖЕНИЯ ЛОКАЦИИ (location reference)!
  * НЕ описывай позиции предметов интерьера в промпте явно — вместо этого используй location reference для сохранения их точного расположения
  * Если предмет упоминается в shot_description — используй его ТОЛЬКО для описания взаимодействия персонажа с ним, НЕ меняя его позицию (если это не указано явно)
  * ПРАВИЛО: "Use location reference image for exact positioning of furniture and interior objects; keep all objects in their original positions from the location reference unless explicitly changed in shot_description"
  * Если shot_description ЯВНО указывает изменение позиции предмета ("стол передвинули", "стул убрали") → тогда можно изменить
  * Если shot_description ЯВНО указывает новый предмет, которого нет в location reference → можно добавить
  * ЦЕЛЬ: обеспечить стабильность расположения предметов между кадрами, используя location reference как источник истины для геометрии интерьера
  * ПРИМЕРЫ:
    - ❌ ПЛОХО: "стол справа от окна" → это переопределяет позицию из reference
    - ✅ ПРАВИЛЬНО: "Use location reference for table position" или просто не упоминать позицию, полагаясь на reference
    - ✅ ПРАВИЛЬНО: Если shot_description говорит "передвинули стол" → тогда указать новую позицию
- **ZONE DISAMBIGUATION (ОБЯЗАТЕЛЬНО)**:
  * Если anchor object потенциально может находиться в разных зонах локации, ты ОБЯЗАН явно указать ЗОНУ через топологию пространства, а не через абстрактное "в центре кадра".
  * "В центре кадра" НЕ считается зоной. Нужна именно топология сцены/пространства.
  * Определи зону по `shot_description` и `location_context.key_objects` (и их смыслу), не выдумывая новых объектов.
  * **ДОКАЗАТЕЛЬСТВА ЗОНЫ (ОБЯЗАТЕЛЬНО)**: добавь 1–2 визуальных маркера, которые подтверждают выбранную зону именно для этой локации и этого шота.

**[P1 CRITICAL] SUBJECT ZONE LOCK:**
- Зона главного субъекта должна следовать текущему `shot_description` и `shot_frame_spec`, а не типовым шаблонам сцены.
- Если вторичные объекты/зоны нужны только как фоновые якоря, удерживай их фоном и не превращай их в опору для постановки субъекта без основания в данных шота.
- Проверяй, что зона субъекта, опора, дистанция и фон согласованы между собой физически.
- **ORIENTATION / ATTENTION TARGET (ОБЯЗАТЕЛЬНО)**:
  * Всегда явно фиксируй **куда повернут корпус и взгляд** персонажа: facing_camera / profile / facing_away / three_quarter.
  * Всегда укажи **attention target** (на что смотрит/к чему обращён): выбери из `shot_description` или 1–2 `location_context.key_objects`.
  * Всегда укажи **attention_target_relation** относительно персонажа: in_front_of_subject / behind_subject / left_of_subject / right_of_subject / above_subject / below_subject.
  * Ориентация должна быть **логически совместима** с `camera_position` и attention target:
  * **КРИТИЧНО - ПЕРСОНАЖИ СМОТРЯТ ДРУГ НА ДРУГА, НЕ В КАМЕРУ (START):**
    * Если в shot_description описано взаимодействие между персонажами → персонажи ОБЯЗАНЫ смотреть друг на друга!
    * Явно укажи в промпте: "[Персонаж A] смотрит на [Персонаж B]", "их лица обращены друг к другу", "facing each other"
    * НЕ используй формулировки "смотрят в камеру", "looking at camera", "facing camera" если shot_description подразумевает, что они смотрят друг на друга
    * Если `character_orientation` = "facing_each_other" (из technical params) → обязательно используй это в промпте!
    - camera_position=in_front → по умолчанию three_quarter; `facing_camera` только если shot_description явно требует прямого фронтального взгляда в объектив/зрителя
    - camera_position=side → profile/three_quarter
    - camera_position=behind → facing_away
  * **ДЕТЕРМИНИРОВАННЫЕ СООТВЕТСТВИЯ:**
    - camera_position=in_front + attention_target_relation=in_front_of_subject → orientation MUST be facing_camera or three_quarter.
    - camera_position=in_front + attention_target_relation=behind_subject → orientation SHOULD be facing_away or profile.
    - camera_position=in_front + attention_target_relation=left_of_subject/right_of_subject → orientation SHOULD be three_quarter or profile (в сторону attention target).
    - camera_position=side → orientation SHOULD be profile/three_quarter (в сторону attention target).
    - camera_position=behind → orientation MUST be facing_away (если это не POV).
  * "Спиной" или "в профиль" разрешено ТОЛЬКО если это следует из camera_position/POV/shot_description (а не случайно).
- **ЕСЛИ ЕСТЬ CONTINUITY REFERENCE**: 
  * ОБЯЗАТЕЛЬНО включи ТОЧНЫЙ путь "{continuity_ref_path}" КАК ПЕРВЫЙ элемент в reference_image_paths
  * Начни english_prompt с "{edit_prefix}" (continuity reference)
  * В поле reference_roles_instruction (отдельно от english_prompt): "image 1 as continuity reference (pose+composition+lighting, одежда, прическа, предметы); image 2 as character — [имя]; image 3 as background — [локация]"
  * Добавь остальные character/location референсы как дополнительные (начиная с image 2)
- **ЕСЛИ НЕТ CONTINUITY REFERENCE**: Начни с "{create_prefix}" и создай изображение с нуля, используя character/location references
- СТРУКТУРИРОВАННЫЙ ШАБЛОН: обязательно включи все 6 компонентов (Действие + Объект + Позиция + Стиль + Освещение + Перспектива)
- Учитывай время суток и освещение из контекста с конкретными параметрами (тип света, температура K, направление)
- Отражай эмоциональную атмосферу из настроения сцены
- Соблюдай визуальный стиль проекта
- Обеспечивай преемственность с предыдущими кадрами
- Подготавливай композицию для следующего кадра
- Соблюдай ПРАВИЛО ИМЁН И КОЛИЧЕСТВА (имена строго как в start_summary.characters)
- **STATE ENFORCEMENT (START T=0)**: If action is transformative (e.g. "tongue shoots out"), explicitly describe the *BEFORE* state (e.g. "mouth slightly open, tongue hidden inside"). Do NOT describe the action itself, only the potential.

**EDGE-CASE ПРАВИЛА ДЛЯ ЭТОГО ШОТА (определены автоматически, следуй строго):**
{json.dumps(shot_type_info, ensure_ascii=False, indent=2)}
Если `edge_case_rules` пуст — это стандартный шот, специальных правил нет.
Если есть правила — следуй полям `start_t0` и `end_tfinal` для определения состояния START и END.

ПРАВИЛА ДЛЯ VIDEO_PROMPT (черновой, будет перегенерирован позже с учётом END):
- English-only, одна строка. Формат: Camera (с позицией) → Subject (quality keyword + action) → Environment (микродинамика) → Tempo
- Camera: словарь Static/Pan/Tilt/Dolly/Tracking/Zoom/Crane + позиция ("at eye level"/"from behind"). Движение камеры: укажи тип + направление. Статика: "Camera locks off at [position]"
- Subject: quality keyword (natural/energetic/slow and deliberate/graceful/confident/fluid movement) + действие (до 2 шагов через "then")
- Environment: ЗАПРЕЩЕНО "static background". Используй: mist drifts / reflections shimmer / leaves sway / fabric billows
- Tempo: slowly/quickly/gently/steadily/handheld/locks off
- Structure: "Scene begins with [START], then [transition], finally ending with [END]"
- Subject = из start_summary.characters. Не подменяй субъектом из continuity reference.
- НЕ выдумывай объекты/одежду/текст, которых нет в shot_description.

ЗАДАЧА ДЛЯ CHARACTERS:
- Верни ПОЛНЫЙ список имён персонажей из start_summary.characters, которые присутствуют в ЭТОМ КОНКРЕТНОМ кадре
- Если в start_summary.characters указан список персонажей, а в кадре присутствует только один из них — верни только того, кто реально виден
- Если в кадре присутствуют все персонажи из start_summary.characters - верни полный список
- ВСЕГДА используй ТОЧНЫЕ имена из start_summary.characters (не изменяй регистр, не добавляй/убирай символы)

ЗАДАЧА ДЛЯ ENGLISH_PROMPT:
- Пиши `english_prompt` и `negative_prompt` строго на {prompt_language_label}; допустимы только стандартные camera/lens термины и авторизованные readable texts.
- **ОБЯЗАТЕЛЬНО ИСПОЛЬЗУЙ ВСЕ ДЕТАЛИ**: initial_state_summary + spatial_composition + camera_position + character_orientation + point_of_view + prop_continuity
- **DIALOGUE IS AUDIO-ONLY**: реплики/междометия/крики персонажей передавай через мимику и артикуляцию рта/позу,
  но НЕ рендери буквенный текст в кадре (no speech bubbles, no subtitle/caption text, no on-image dialogue words).
- **КРИТИЧНО - ИСПОЛЬЗУЙ ОПИСАНИЕ ЛОКАЦИИ ИЗ location_context**:
  * ОБЯЗАТЕЛЬНО используй lighting из location_context (тип света, температуру, направление)
  * ОБЯЗАТЕЛЬНО используй color_palette из location_context (цветовую палитру)
  * ОБЯЗАТЕЛЬНО используй atmosphere из location_context (атмосферу)
  * {start_key_objects_rule}
  * НЕ придумывай свои характеристики освещения или цветов - используй ТОЛЬКО из location_context!
  * ПРИМЕР: если location_context.lighting = "яркий белый свет", НЕ пиши "слабое освещение" или "тёмный зал"
- **КРИТИЧНО - LOCATION REFERENCE - MULTI-VIEW SHEET**:
  * Если location reference — multi-view, НЕ описывай "4 панели/лист/разбивку" в основном тексте промпта (это триггерит коллаж).
  * Вместо этого: опиши кадр как ЕДИНЫЙ непрерывный фрейм и добавь короткую фразу: "Use the location reference to ground geometry/palette; select ONE view that matches this shot; do not output a split-screen unless storyboard explicitly requires it."
- **СТРУКТУРА ПРОМПТА**:
  1. **[ВИЗУАЛЬНОЕ ОПИСАНИЕ]** - одна компактная команда с ключевыми элементами:
     * Формат: "[shot type]: [character state + props] [position]. [environment]. [atmosphere]. [lighting из location_context]. Палитра: [color_palette из location_context]."
     * Объединяй близкие фразы: вместо "Съёмка с низким уровнем освещения создаёт глубокие тени и мрачную атмосферу" → "Низкое освещение: глубокие тени, мрачная атмосфера"
     * Убирай избыточные слова: вместо "находится на расстоянии миллиметра над" → "в миллиметре над"
     * Не повторяй информацию об освещении/палитре - укажи один раз
  2. **[ТЕХНИЧЕСКИЕ ПАРАМЕТРЫ]** - компактно, без лишних слов:
     * Формат: "Ракурс: [camera_position]. [focal_length] [aperture], [camera_model]. Стиль: [стиль], [детали стиля]."
     * Убирай слова "Кинематографический стиль", "Съёмка с" - это избыточно
     * Пример: "Низкий ракурс снизу вверх. 24 мм f/8.0, Arri Alexa Mini LF. Стиль: [название из visual_style], мягкие края, зернистость плёнки."
- **НЕ РАЗБИВАЙ НА МНОЖЕСТВЕННЫЕ КОМАНДЫ**: Create, Position, Show, Apply, Add — используй ОДНУ команду для всего
- **ЦЕЛЬ**: промпт должен сохранять всю необходимую информацию. Убирай избыточные слова и повторы.

Создай кинематографичные промпты КАК КОМАНДЫ РЕДАКТИРОВАНИЯ, учитывая весь контекст и определи необходимость end кадра.
НЕ ИСПОЛЬЗУЙ ПРИМЕРЫ В ВЫВОДЕ, СТРОЙ КАДР НА ОСНОВЕ ПЕРЕДАННОГО ОПИСАНИЯ!
"""

    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_ultimate,
            max_tokens=8000,
            temperature=0.4,
            response_format={"type": "json_object"},
        )
        
        # Логируем исходный ответ для диагностики
        if response:
            logger.debug(f"🔍 Исходный ответ модели (первые 500 символов): {response[:500]}")
        else:
            logger.warning("⚠️ Модель вернула пустой ответ")
        
        result = parse_llm_json(response)
        if not isinstance(result, dict):
            logger.error(f"❌ Художественная генерация вернула {type(result).__name__} вместо dict: {str(result)[:200]}")
            return None
        return result

    except json.JSONDecodeError as e:
        logger.error(f"❌ Ошибка парсинга JSON в художественной генерации промптов: {e}")
        logger.error(f"📄 Исходный текст ответа:\n{response if 'response' in locals() else 'Ответ не получен'}")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка художественной генерации промптов: {e}")
        if 'response' in locals():
            logger.error(f"📄 Исходный текст ответа:\n{response}")
        return None


def _generate_shot_prompt(
    extended_context: Dict[str, Any],
    shot_type: str,
    video_prompt: str = "",
    start_llm_result: Optional[Dict[str, Any]] = None,
    language: str = "en",
) -> Optional[Dict[str, Any]]:
    """
    Генерирует промпт для кадра через LLM.
    Для START и END кадров использует двухэтапный подход с расширенным контекстом.
    Поздние LLM-санитайзеры здесь намеренно не запускаются:
    финальная смысловая валидация/ремонт выполняется в shots_prompt_qa_tool.
    """
    shot_frame_spec = extended_context.get("shot_frame_spec")
    if not isinstance(shot_frame_spec, dict) or not shot_frame_spec.get("primary_subject") or not shot_frame_spec.get("must_show"):
        logger.error("❌ Shot generation aborted: missing or invalid shot_frame_spec for shot '%s'", extended_context.get("shot_description", ""))
        return None

    start_state_spec = shot_frame_spec.get("start_state_spec")
    end_state_spec = shot_frame_spec.get("end_state_spec")
    if not isinstance(start_state_spec, dict) or not start_state_spec:
        logger.error(
            "❌ Shot generation aborted: shot_frame_spec.start_state_spec missing/invalid for shot '%s'",
            extended_context.get("shot_description", ""),
        )
        return None
    if not isinstance(end_state_spec, dict) or not end_state_spec:
        logger.error(
            "❌ Shot generation aborted: shot_frame_spec.end_state_spec missing/invalid for shot '%s'",
            extended_context.get("shot_description", ""),
        )
        return None

    def _build_phase_context(phase: str) -> Dict[str, Any]:
        phase_context = dict(extended_context)
        phase_context["full_shot_frame_spec"] = shot_frame_spec
        if phase == "start":
            phase_context["shot_frame_spec"] = start_state_spec
        elif phase == "end":
            phase_context["shot_frame_spec"] = end_state_spec
        else:
            phase_context["shot_frame_spec"] = shot_frame_spec
        phase_context["transition_spec"] = shot_frame_spec.get("transition_spec", {})
        return phase_context

    def _merge_structured_fields(
        llm_result: Dict[str, Any],
        source_params: Dict[str, Any],
        *,
        final_fields: bool = False,
    ) -> Dict[str, Any]:
        merged = dict(llm_result or {})

        def _is_empty(value: Any) -> bool:
            if value is None:
                return True
            if isinstance(value, str):
                return not value.strip()
            if isinstance(value, (list, dict, tuple, set)):
                return len(value) == 0
            return False

        def _looks_like_placeholder(value: Any) -> bool:
            """Эвристика для случаев, когда LLM вернула сам placeholder из шаблона промпта
            (например 'shot_frame_spec.primary_subject' или 'end_technical_params.main_subject')
            вместо реального значения."""
            if not isinstance(value, str):
                return False
            stripped = value.strip()
            if not stripped or " " in stripped:
                return False
            return bool(re.match(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+$", stripped))

        if _is_empty(merged.get("characters")) and not _is_empty(source_params.get("characters")):
            merged["characters"] = source_params.get("characters", [])

        if _is_empty(merged.get("main_subject")) or _looks_like_placeholder(merged.get("main_subject")):
            merged["main_subject"] = (
                source_params.get("main_subject")
                or shot_frame_spec.get("primary_subject", "")
            )

        if final_fields:
            authoritative_final_fields = (
                "final_shot_size",
                "framing_delta_percent",
                "subject_scale_ratio",
            )
            for field in authoritative_final_fields:
                if not _is_empty(source_params.get(field)):
                    merged[field] = source_params.get(field)

            final_map = (
                "final_shot_size",
                "final_camera_angle",
                "final_lighting_style",
                "final_color_palette",
                "final_camera_position",
                "final_character_orientation",
                "final_spatial_composition",
                "final_point_of_view",
                "spatial_changes_from_start",
                "camera_movement_completed",
                "composition_stability",
                "continuity_score",
                "next_shot_compatibility",
                "framing_delta_percent",
                "subject_scale_ratio",
                "prop_continuity",
                "final_camera_yaw_deg",
                "final_camera_pitch_deg",
                "final_subject_yaw_deg",
                "final_focus_target",
                "final_depth_of_field",
                "final_depth_order",
            )
            for field in final_map:
                if _is_empty(merged.get(field)) and not _is_empty(source_params.get(field)):
                    merged[field] = source_params.get(field)
        else:
            start_map = (
                "camera_position",
                "character_orientation",
                "spatial_composition",
                "point_of_view",
                "initial_state_summary",
            )
            for field in start_map:
                if _is_empty(merged.get(field)) and not _is_empty(source_params.get(field)):
                    merged[field] = source_params.get(field)

        return merged
    
    if shot_type == "start":
        start_context = _build_phase_context("start")
        # Двухэтапный подход для START кадров с расширенным контекстом
        logger.info("🔧 Этап 1: Технический анализ START кадра")
        
        # Первый этап: технический анализ
        technical_params = _analyze_shot_technical(start_context)
        
        if not technical_params:
            logger.error("❌ Не удалось выполнить технический анализ START кадра")
            return None
        
        logger.info("🎨 Этап 2: Художественная генерация START промптов")
        
        # Второй этап: художественная генерация
        artistic_result = _generate_shot_artistic(technical_params, start_context, language=language)
        
        if not artistic_result:
            logger.error("❌ Не удалось выполнить художественную генерацию START промптов")
            return None
        artistic_result = _merge_structured_fields(artistic_result, technical_params, final_fields=False)
            
        # ПРИНУДИТЕЛЬНО добавляем continuity reference, если есть
        continuity_ref_path = start_context.get('continuity_reference_path')
        if continuity_ref_path:
            # Получаем текущие референсы от LLM
            current_refs = artistic_result.get('reference_image_paths', [])
            
            # Если LLM не включил continuity reference - добавляем принудительно
            if continuity_ref_path not in current_refs:
                logger.info(f"🔧 CONTINUITY FIX: Принудительно добавляем continuity reference: {continuity_ref_path}")
                artistic_result['reference_image_paths'] = [continuity_ref_path] + current_refs
                
                # Обновляем role mapping для continuity reference
                current_roles = artistic_result.get('reference_roles_instruction', '')
                artistic_result['reference_roles_instruction'] = f"image 1 as continuity reference; {current_roles}"
                
                # Обновляем english_prompt для использования language-aware continuity prefix
                current_prompt = artistic_result.get('english_prompt', '')
                continuity_edit_prefix = _get_prompt_edit_prefix(language)
                if not current_prompt.startswith(continuity_edit_prefix):
                    artistic_result['english_prompt'] = f"{continuity_edit_prefix} {current_prompt}"
                    
            logger.info(f"🔗 CONTINUITY DEBUG: LLM received continuity reference: {continuity_ref_path}")
            logger.info(f"🔗 CONTINUITY DEBUG: LLM result refs: {len(artistic_result.get('reference_image_paths', []))}")
            
        return artistic_result
    
    # Для END кадров используем новый двухэтапный подход
    elif shot_type == "end":
        end_context = _build_phase_context("end")
        if not start_llm_result:
            logger.error("❌ Для генерации END кадра требуется start_llm_result")
            return None
            
        logger.info("🔧 Этап 1: Технический анализ END кадра")
        
        # Первый этап: технический анализ END
        end_technical_params = _analyze_end_shot_technical(
            start_technical_params=start_llm_result,
            video_prompt=video_prompt,
            extended_context=end_context
        )
        
        if not end_technical_params:
            logger.error("❌ Не удалось выполнить технический анализ END кадра")
            return None
    
        # Нормализация финальной крупности по ratio/delta и стартовой крупности
        def _calculate_shot_size_label(start_label: str, delta_percent: Optional[float], ratio: Optional[float]) -> str:
            canonical = [
                "Extreme wide",
                "Wide shot",
                "Medium wide",
                "Medium shot",
                "Medium close-up",
                "Close-up",
                "Extreme close-up",
            ]
            # Нормализация входного ярлыка к ближайшему канону
            s = (start_label or "").strip().lower()
            def map_to_index(name: str) -> int:
                n = name
                if "extreme" in n and ("close" in n or "ec" in n):
                    return 6
                if ("close" in n and "extreme" not in n) or "cu" in n:
                    return 5
                if "medium" in n and ("close" in n or "mc" in n):
                    return 4
                if ("medium" in n and ("wide" not in n and "close" not in n)) or "ms" in n:
                    return 3
                if ("medium" in n and "wide" in n) or "mw" in n:
                    return 2
                if ("wide" in n and "extreme" not in n) or "ws" in n:
                    return 1
                if "extreme" in n and "wide" in n:
                    return 0
                # По умолчанию — средний
                return 3
            start_idx = map_to_index(s)

            def _coerce_float(value: Any, default: float) -> float:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return default

            steps = 0
            r = _coerce_float(ratio, 1.0)
            d = _coerce_float(delta_percent, 0.0)
            # Приоритет ratio, затем delta
            if r >= 1.6:
                steps = +2
            elif r >= 1.3:
                steps = +1
            elif r <= 0.6:
                steps = -2
            elif r <= 0.8:
                steps = -1
            else:
                if d >= 40:
                    steps = +2
                elif d >= 25:
                    steps = +1
                elif d <= -40:
                    steps = -2
                elif d <= -25:
                    steps = -1
                else:
                    steps = 0

            final_idx = min(max(start_idx + steps, 0), len(canonical) - 1)
            return canonical[final_idx]

        # БАЗОВАЯ крупность шота = storyboard.camera_plan (для START это истина).
        # Для END крупность может меняться, если video_prompt описывает движение камеры (dolly/zoom),
        # поэтому END-ярлык считаем из ratio/delta относительно базовой крупности.
        def _camera_plan_to_base_label(camera_plan: str, fallback_label: str) -> str:
            cp = (camera_plan or "").strip().lower()
            if "extreme wide" in cp:
                return "Extreme wide"
            if "wide" in cp:
                return "Wide shot"
            if "medium" in cp:
                return "Medium shot"
            if "extreme close" in cp:
                return "Extreme close-up"
            if "close" in cp:
                return "Close-up"
            return fallback_label

        base_start_label = _camera_plan_to_base_label(
            end_context.get("camera_plan", ""),
            start_llm_result.get("shot_size", "") or "Medium shot",
        )

        normalized_final_size = _calculate_shot_size_label(
            start_label=base_start_label,
            delta_percent=end_technical_params.get("framing_delta_percent"),
            ratio=end_technical_params.get("subject_scale_ratio")
        )

        def _needs_physical_relation_legibility() -> bool:
            shot_frame_spec = end_context.get("shot_frame_spec", {}) or {}
            end_state_spec = shot_frame_spec.get("end_state_spec", {}) or {}
            world_physics = end_state_spec.get("world_physics", {}) or shot_frame_spec.get("world_physics", {}) or {}

            must_show_blob = " ".join(str(x or "") for x in (end_state_spec.get("must_show") or shot_frame_spec.get("must_show") or []))
            physics_blob = " ".join(
                str(world_physics.get(k, "") or "")
                for k in ["support_state", "surface_state", "body_relation", "stability"]
            )
            full_blob = f"{must_show_blob} {physics_blob}".lower()

            face_readable = bool(end_state_spec.get("facial_expression") or end_state_spec.get("gaze_direction"))
            support_terms = [
                "опор", "support", "surface", "floor", "плит", "slab",
                "люк", "trap", "opening", "hole", "edge", "край", "разрыв", "gap", "провал",
            ]
            unstable_terms = [
                "unsupported", "precarious", "без контакта", "потер", "исчез", "ушла вниз",
                "провалил", "разрыв", "нестабил", "утрата опоры",
            ]
            has_support_relation = any(term in full_blob for term in support_terms)
            has_instability = any(term in full_blob for term in unstable_terms)
            return bool(face_readable and has_support_relation and has_instability)

        if _needs_physical_relation_legibility():
            size_relax_map = {
                "Extreme close-up": "Close-up",
                "Close-up": "Medium close-up",
            }
            relaxed_size = size_relax_map.get(normalized_final_size, normalized_final_size)
            if relaxed_size != normalized_final_size:
                logger.info(
                    "🎯 END PHYSICAL LEGIBILITY: relaxing final_shot_size %s -> %s to preserve subject/support relation",
                    normalized_final_size,
                    relaxed_size,
                )
                normalized_final_size = relaxed_size
                try:
                    ratio_val = float(end_technical_params.get("subject_scale_ratio", 1.0) or 1.0)
                except Exception:
                    ratio_val = 1.0
                try:
                    delta_val = int(end_technical_params.get("framing_delta_percent", 0) or 0)
                except Exception:
                    delta_val = 0
                end_technical_params["subject_scale_ratio"] = min(ratio_val, 1.45)
                end_technical_params["framing_delta_percent"] = min(delta_val, 35)

        # Записываем нормализованный ярлык в технические параметры
        end_technical_params["final_shot_size"] = normalized_final_size

        logger.info("🎨 Этап 2: Художественная генерация END промптов")
        
        # Второй этап: художественная генерация END
        end_artistic_result = _generate_end_shot_artistic(
            end_technical_params=end_technical_params,
            start_llm_result=start_llm_result,
            video_prompt=video_prompt,
            extended_context=end_context,
            final_shot_size_label=normalized_final_size,
            location_context=(start_llm_result.get("location_context") or {}),
            language=language,
        )
        
        if not end_artistic_result:
            logger.error("❌ Не удалось выполнить художественную генерацию END промптов")
            return None
        final_end = _merge_structured_fields(end_artistic_result, end_technical_params, final_fields=True)

        # ------------------------------------------------------------
        # CONTINUITY FALLBACK:
        # END-кадр наследует персонажей из START, если модель их "забыла".
        # Это критично для:
        # - консистентности стиля/идентичности
        # - корректной сборки reference_image_paths (персонажные референсы)
        # ------------------------------------------------------------
        try:
            end_chars = final_end.get("characters", None)
            if not end_chars or (isinstance(end_chars, list) and len(end_chars) == 0):
                start_chars = start_llm_result.get("characters", None)
                if isinstance(start_chars, list) and start_chars:
                    final_end["characters"] = start_chars
                    logger.info("🔁 END CONTINUITY FIX: наследуем characters из START (model returned empty)")
                else:
                    # Консервативный fallback: НЕ тащим всех персонажей сцены (это вызывает смысловую подмену субъекта).
                    # Вместо этого — пытаемся извлечь только тех, кто явно упомянут в shot_description.
                    shot_desc = str(end_context.get("shot_description", "") or "")
                    scene_chars = end_context.get("scene_characters", None)
                    inferred: List[str] = []
                    if shot_desc and isinstance(scene_chars, list) and scene_chars:
                        sd_l = shot_desc.lower()
                        for nm in scene_chars:
                            n = str(nm or "").strip()
                            if not n:
                                continue
                            if n.lower() in sd_l:
                                inferred.append(n)
                    if inferred:
                        final_end["characters"] = inferred
                        logger.info("🔁 END CONTINUITY FIX: inferred characters from shot_description=%s", inferred)
                    else:
                        logger.info("🔁 END CONTINUITY FIX: no characters inferred; keep empty (avoid subject drift)")
        except Exception as e:
            logger.warning(f"⚠️ END CONTINUITY FIX: не удалось наследовать characters: {e}")
        
        return final_end


def _validate_video_prompt_structure(video_prompt: str) -> Optional[str]:
    """
    Возвращает None если video_prompt соответствует обязательному формату
    "[CAMERA]; [SUBJECT]; [ENVIRONMENT]; [TEMPO]" (4 непустых сегмента через ';').
    Иначе возвращает короткую диагностику.
    """
    if not isinstance(video_prompt, str):
        return f"video_prompt is not a string (got {type(video_prompt).__name__})"
    segments = [s.strip() for s in video_prompt.split(";")]
    if len(segments) != 4:
        return f"expected 4 segments separated by ';', got {len(segments)}"
    empty_indices = [i for i, s in enumerate(segments) if not s]
    if empty_indices:
        labels = ["CAMERA", "SUBJECT", "ENVIRONMENT", "TEMPO"]
        empty_labels = ", ".join(labels[i] for i in empty_indices)
        return f"empty segment(s): {empty_labels}"
    return None


def _generate_transition_video_prompt(
    start_llm_result: Dict[str, Any],
    end_llm_result: Dict[str, Any],
    extended_context: Dict[str, Any],
) -> Optional[str]:
    """
    Генерирует video_prompt ПОСЛЕ того, как уже известны START и END image-prompts.
    Video prompt хранится только у START кадра.
    """
    shot_frame_spec = extended_context.get("shot_frame_spec", {}) or {}
    transition_spec = shot_frame_spec.get("transition_spec", {}) or extended_context.get("transition_spec", {}) or {}

    # Определяем тип шота для edge-case правил
    shot_type_info = _classify_shot_type(
        extended_context.get("shot_description", ""),
        extended_context.get("camera_plan", ""),
    )

    system_prompt = """
Ты создаешь ТОЛЬКО video_prompt для одного storyboard-shot. Верни строго JSON: {"video_prompt": "single-line video prompt"}

SOURCE OF TRUTH: shot_description + camera_plan + transition_spec + готовые START/END image-prompts.

ОБЯЗАТЕЛЬНЫЙ ФОРМАТ OUTPUT (точно 4 сегмента через ";"):
"[CAMERA clause]; [SUBJECT quality+action]; [ENVIRONMENT micro-dynamics]; [TEMPO]"

СЕГМЕНТ 1 — CAMERA:
- Словарь: Static / Pan L/R / Tilt U/D / Dolly In/Out / Tracking / Zoom In/Out / Crane
- Обязательна ПОЗИЦИЯ: "at eye level" / "from behind" / "from low angle"
- Статика: "Camera locks off at [position]"
- Движение: "Camera [type] [direction] at [position]" (пример: "Camera sharp zoom in at eye level")

СЕГМЕНТ 2 — SUBJECT:
- [quality keyword] + действие через "then" (до 2 шагов)
- Quality keywords (выбери ОДИН): natural / energetic / slow and deliberate / graceful / confident / fluid movement
- Направление: "спускается"→"descends" (НИКОГДА "ascends"). "Касается"→"makes contact" (НЕ "approaching")
- Формат: "subject with [quality] movement [action], then [action2]"

СЕГМЕНТ 3 — ENVIRONMENT:
- ОБЯЗАТЕЛЬНА микродинамика: dust swirls / torch flames flicker / mist drifts / reflections shimmer
- ЗАПРЕЩЕНО: "static background"

СЕГМЕНТ 4 — TEMPO:
- Выбери ОДИН: slowly / quickly / sharply / gently / steadily / abruptly

ПРАВИЛА:
- video_prompt = ОДНА строка, English-only, NO Cyrillic. Никакого markdown и prose.
- НЕ выдумывай объекты/одежду/текст, которых нет в shot_description.
- Close-up: фон = blur-hint. Split-screen: обязателен токен "split-screen".
- transition_spec.physics_delta → физическая дельта. transition_spec.affect_delta → дельта лица/взгляда.
- Субъект = из START/END main_subject. Не подменяй.

SELF-CHECK: 1) каждый noun phrase поддержан shot_description? 2) все 4 сегмента присутствуют? 3) quality keyword есть?
"""
    payload = {
        "camera_plan": extended_context.get("camera_plan", ""),
        "shot_description": extended_context.get("shot_description", ""),
        "shot_frame_spec": shot_frame_spec,
        "transition_spec": transition_spec,
        "start_english_prompt": start_llm_result.get("english_prompt", ""),
        "end_english_prompt": end_llm_result.get("english_prompt", ""),
        "start_main_subject": start_llm_result.get("main_subject", shot_frame_spec.get("primary_subject", "")),
        "end_main_subject": end_llm_result.get("main_subject", shot_frame_spec.get("primary_subject", "")),
        "edge_case_rules": shot_type_info.get("edge_case_rules", []),
    }
    try:
        response = call_openai_api(
            prompt="INPUT:\n" + json.dumps(payload, ensure_ascii=False),
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=1200,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        parsed = parse_llm_json(response)
        video_prompt = str((parsed or {}).get("video_prompt", "") or "").strip()
        if not video_prompt:
            return None
        video_prompt = " ".join(video_prompt.split())

        structure_error = _validate_video_prompt_structure(video_prompt)
        if structure_error:
            logger.warning(
                "video_prompt structure invalid (%s); retrying with corrective hint",
                structure_error,
            )
            corrective_payload = dict(payload)
            corrective_payload["previous_invalid_video_prompt"] = video_prompt
            corrective_payload["structure_violation"] = structure_error
            corrective_payload["instruction"] = (
                "Previous output failed structure check. Return EXACTLY 4 non-empty "
                "segments separated by ';' in the order CAMERA; SUBJECT; ENVIRONMENT; TEMPO."
            )
            retry_response = call_openai_api(
                prompt="INPUT:\n" + json.dumps(corrective_payload, ensure_ascii=False),
                system_prompt=system_prompt,
                model=model_hard,
                max_tokens=1200,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            retry_parsed = parse_llm_json(retry_response)
            retry_vp = str((retry_parsed or {}).get("video_prompt", "") or "").strip()
            if retry_vp:
                retry_vp = " ".join(retry_vp.split())
                retry_error = _validate_video_prompt_structure(retry_vp)
                if retry_error is None:
                    return retry_vp
                logger.warning(
                    "video_prompt structure still invalid after retry (%s); keeping original",
                    retry_error,
                )

        return video_prompt
    except Exception as e:
        logger.error("❌ Ошибка генерации финального video_prompt: %s", e)
        return None

def _generate_end_shot_artistic(
    end_technical_params: Dict[str, Any],
    start_llm_result: Dict[str, Any],
    video_prompt: str,
    extended_context: Dict[str, Any],
    final_shot_size_label: Optional[str] = None,
    location_context: Optional[Dict[str, Any]] = None,
    language: str = "en",
) -> Optional[Dict[str, Any]]:
    """
    Художественная генерация END кадра на основе технического анализа.
    Создает финальные промпты и принимает решения о линковке.
    """
    
    prompt_language_label = _get_prompt_language_label(language)
    edit_prefix = _get_prompt_edit_prefix(language)

    system_prompt = (
        _build_state_to_image_common_core(
            phase_name="END",
            state_spec_name="end_state_spec",
            composition_field="final_spatial_composition",
            camera_position_field="final_camera_position",
            orientation_field="final_character_orientation",
            summary_field="final_spatial_composition",
        )
        + f"""
=== СЛОЙ A: РОЛЬ И КОНТРАКТ ===

Ты -- креативный директор. Создай финальный english_prompt для END кадра (T=final) и определи тип связи со следующим кадром.

**LANGUAGE CONTRACT:**
- `english_prompt`, `negative_prompt`, `reference_roles_instruction`, `main_subject`, `final_spatial_composition` -- строго на {prompt_language_label}.
- `video_prompt` -- ТОЛЬКО на английском языке.
- Для END-редактирования используй префикс `{edit_prefix}`.

**ГЛОССАРИЙ:** shot_size = крупность плана; ratio = коэффициент масштабирования; english_prompt = ОДНА команда редактирования; linking = решение о преемственности.

**[P0 CRITICAL] SHOT_FRAME_SPEC = SOURCE OF TRUTH:**
- `shot_frame_spec` -- авторитетная спецификация шота. Поля `primary_subject`, `visible_characters`, `must_show`, `must_not_show`, `visible_readable_texts`, `hidden_readable_texts`, `world_physics` обязательны.
- `facial_expression`, `gaze_direction`, `pose_signature` из `shot_frame_spec`/`end_state_spec` обязательны; не сводить к нейтральной позе/лицу.
- Нельзя добавлять объекты/персонажей вне `must_show` и `shot_description`. Все structured fields непустые и согласованы.

**[P0 CRITICAL] STORYBOARD LOCKS:**
- camera_plan и shot_description -- ИСТИНА для текущего кадра. END = финальный стоп-кадр ЭТОГО shot_number.
- Финальная крупность следует `final_shot_size` из technical params (рассчитан из ratio/delta), не "застревает" на camera_plan.
- PRIMARY SUBJECT LOCK: субъект = shot_description этого кадра. Не подменять из других кадров.
- ENTITY NATURE LOCK: нечеловеческий персонаж сохраняет свои признаки (scales/fur/metal из immutable_attributes). Человек -- без животных/роботических признаков.
- NAME LOCK: канонические имена из storyboard/scene_characters. Не искажать, не переименовывать в вид/роль.

**[P0 CRITICAL] ENGLISH_PROMPT STRUCTURE:**
1. [Editing instruction]: "Edit image 1: TRANSFORM/Change/Remove/Add..." -- IMPERATIVE VERBS. ЗАПРЕЩЕНО: "Show...", "Image of...", "Scene with..."
2. [Subject]: результат действия (T=final, ПОСЛЕ факта). ЗАПРЕЩЕНО: процессные глаголы (extending, moving, starts to).
3. [Composition]: [final_shot_size] + масштаб если ratio!=1.0
4. [Location]: измененное окружение (если есть)
5. [Style]: "Ракурс: [camera_position]. [focal_length] [aperture], [camera_model]. Стиль: [стиль]."
6. [Action result]: последствия движения
- Если ratio!=1.0: техническая команда ПЕРВОЙ ("Zoom in (scale Xx) to [final_shot_size]..." / "Pull back (scale Xx)...")
- Если yaw/pitch!=0: "Shift perspective [direction] by [degrees] to reveal [new elements]..."
- Для неизменных частей: "Keep...", "Maintain..."

=== СЛОЙ B: ПОШАГОВЫЙ АЛГОРИТМ ГЕНЕРАЦИИ ===

Выполни по порядку:

**ШАГ 1 -- SUBJECT:** main_subject = `shot_frame_spec.primary_subject`. END = результат shot_description + shot_frame_spec + end_technical_params.

**ШАГ 2 -- T=FINAL STATE:**
- Описывай РЕЗУЛЬТАТ действия (post-fact), НЕ процесс. Любой ing-глагол / "начинает" / "продолжает" из shot_description
  переведи в финальное видимое состояние. Шаблоны (применяй как образец, не как фильтр):
    "reaching"  → "hand fully extended toward [target], fingers [closed/spread]"
    "falling"   → "mid-air at [height], body in [orientation], hair/clothes lifted by airflow"
    "running"   → "mid-stride, leading foot lifted, opposite arm forward"
    "grabbing"  → "hand firmly closed around [object], knuckles tense"
    "opening"   → "[door/lid] fully open at [angle], hinge/latch visible"
    "tongue extending" → "tongue fully extended, tip at [target]"
    "shouting"  → "mouth wide open mid-vowel, jaw dropped, no audible glyphs in frame"
- Если video_prompt пуст -- выводи END из shot_description + shot_frame_spec + end_technical_params напрямую.
- Если video_prompt есть -- только transition-подсказка; не переопределяет shot_description.
- `final_spatial_composition` и `final_*` поля = авторитетный финальный стоп-кадр.

**ШАГ 3 -- FACE/GAZE/POSE + WORLD PHYSICS:**
- facial_expression/gaze_direction из end_state_spec обязательны. Не default'и к neutral/blank/facing_camera.
- Если spec пуст, но лицо читается -- выведи микро-реакцию из shot_description + world_physics + transition_spec. affect_delta = дельта между START и END.
- pose_signature -- переведи в видимый результат: корпус, руки, голова, реквизит.
- world_physics = источник истины для физики END (опора, контакт/разрыв, перекрытия). forbidden_implications -- не допускай в промпте.

**ШАГ 5 -- PROP_CONTINUITY:** Порядок: Edit image 1: [final_shot_size]: [масштаб IF ratio!=1.0]; [позиция]; [prop changes]; [освещение].
- removed: "with [item] removed" + negative: "no [item] in [CHARACTER] hands/on [CHARACTER] body" (адресное, не общее).
- added: "add [item] to [position]". kept+state: финальное состояние, без процессных глаголов. Все изменения ОБЯЗАТЕЛЬНО явно в команде!

**ШАГ 6 -- VISUAL DIFFERENCE ENFORCEMENT:**
- END ОБЯЗАН визуально отличаться от START. ЗАПРЕЩЕНО копировать описание START.
- Действие произошло -- ДРУГАЯ позиция/состояние. Камера двигалась -- другой framing.
- Полная статика (delta=0, ratio=1.0) -- ОБЯЗАТЕЛЬНО изменить хотя бы ОДИН элемент как СТАТИЧНЫЙ снимок:
  micro-expression (corner of mouth slightly tighter / eyebrow lifted), light angle (rotated by N degrees),
  atmospheric density (denser smoke / thinner haze), reflections (repositioned by geometry),
  shadows (shifted/elongated), chest fully expanded mid-inhale (как стоп-кадр, НЕ как "breathing" процесс).
- Описывай эти изменения как видимый результат, а НЕ как процесс/анимацию.
- Сила формулировки: используй "noticeably / clearly visible / distinctly" для естественной разницы. НЕ применяй
  "dramatically" к статичной паузе и НЕ преуменьшай через "slightly / barely / subtly".
- Фоновая динамика из video_prompt (falling/drifting/flickering/burning/rising/dispersing/melting) -- зафиксируй РЕЗУЛЬТАТ в END.
- Описывай результат семантически: "hand now grasping firmly" (да), "hand moved 20cm" (нет).

**ШАГ 7 -- CONTINUITY LOCKS (при Edit image 1):**
- Location: сохрани 1-2 фоновых якоря из location_context (не абстрактный градиент). Close-up: якоря как частичные фрагменты, не полный рост.
- Composition: если крупность плотнее -- continuity ref сохраняет identity/pose/wardrobe/lighting/environment, НЕ старый framing.
- Мебель из location_context.key_objects -- из location reference, не переопределяй позиции. Не меняй массовку/одежду/декор без основания.
- Отражения/зеркала -- следуй геометрии финального кадра и world_physics.

**ШАГ 8 -- МАСШТАБИРОВАНИЕ:** ratio>1.05: "Zoom in (scale Xx)"; ratio<0.95: "Pull back (scale Xx)"; ratio=0.98-1.02: НЕ масштабируй (ЗАПРЕЩЕНО: "1.0x"/"0.99x"/"1.01x"). Pan/Tilt: "Shift perspective of [Location] to [direction]" (НЕ "Pan right"), фон меняется геометрически.

**ШАГ 9 -- ЛИНКОВКА:** 1) БЛОКЕРЫ (= both_false, STOP): смена локации | main_subject нет в next_shot | temporal jump | cutaway/insert. 2) score>=8 + stable + delta<=15 + high compat: link_as_next_start=true; иначе reference=true. 3) score 5-7: reference=true. 4) score<5: both_false.

**ШАГ 10 -- NEGATIVE_PROMPT (СТРОГО максимум 20 слов через запятую):** Базовый: blurry, motion blur, watermark. Close-up: + "extra/missing body parts". Prop removed: "no [item] in [CHARACTER] hands". КРИТИЧНО: ОДНА идея = ОДНО слово. НЕ ПОВТОРЯЙ одно и то же разными словами (ошибка: "без другого задника, без абстрактного фона, без замены архитектуры" → исправь: "abstract background").

**ШАГ 11 -- SELF-CHECK (обязательно перед возвратом JSON):**
1. Каждый noun phrase в english_prompt поддержан shot_description/shot_frame_spec? Нет -- удали.
2. english_prompt и final_spatial_composition описывают ОДИН кадр? Расходятся -- согласуй.
3. Процессные глаголы (extending, moving, starts to)? -- замени на результат.
4. END визуально отличается от START? Нет -- добавь видимое изменение.
5. Любое относительное движение субъекта/камеры или непустой `transition_spec.environment_delta` про смену видимого окружения? -- в english_prompt должно быть явное изменение фона/боке/световых акцентов относительно START (параллакс, сдвиг деталей среды или заднего плана в крупном плане), без смены локации без оснований.
6. ratio~1.0 (0.98-1.02) -- нет ли масштабирования в english_prompt? Удали.
7. Все 6 обязательных элементов (instruction/subject/composition/location/style/action result) в english_prompt? Нет -- добавь.

=== СЛОЙ C: СПРАВОЧНЫЕ ТАБЛИЦЫ ===

**УНИВЕРСАЛЬНЫЕ ОГРАНИЧЕНИЯ:**
| Правило | Описание |
|---------|----------|
| NO-INVENTION | Не добавляй объекты/символы/пропсы не из shot_description ЭТОГО шота. Фон -- общий blur-hint. scene.action != список объектов шота |
| НЕ УТВЕРЖДАЙ ОТСУТСТВИЕ | Не пиши "no X / without X" если не требуется shot_description |
| REACTION/SPLIT SCREEN | Реакция на событие, НЕ "looking at camera". REACTION FRAMING LOCK: split-screen close-up, НЕ "Medium shot" |
| SINGLE-FRAME | Краевые фрагменты среды -- часть ТОГО ЖЕ изображения, НЕ inset/cutaway/panel |
| TEXT OVERLAY LOCK | Если visible_readable_texts пуст -- никаких наложенных текстовых подписей/credits/labels |
| OVER-SHOULDER LOCK | Видимые части тела соответствуют character reference (силуэт/пропорции/текстуры) |
| OBJECT-CHARACTER | Если взаимодействие объекта с персонажем -- включи видимые части персонажа в кадр (затылок/шея/лицо) |
| THROW/RELEASE LOCK | Throw verbs -- END = post-release: объект НЕ в руке, по умолчанию OUT OF FRAME. Не выдумывай приземление |
| FALLING PROP LANDING | "Падает на рог/голову" -- END = post-contact, prop resting on body part. Не заменяй на environment upgrades |
| CONTINUITY FACTS | scene_continuity_facts: применяй если не противоречит shot_description и уместно по крупности |
| HALLUCINATION | Не добавляй unrequested objects (stands, furniture). Только props из START или prop_continuity.added |

**ПРИМЕРЫ КОМАНД:**
| Тип | Пример |
|-----|--------|
| Dolly In | "Edit image 1: Zoom in (scale 1.25x) to Close-up. Subject fills more of frame; maintain low-key lighting" |
| Dolly Out | "Edit image 1: Pull back (scale 0.8x) to Wide shot. Subject smaller, more environment; maintain exposure" |
| Action | "Edit image 1: TRANSFORM subject pose: hand now grasping object firmly; maintain lighting" |
| Prop Remove | "Edit image 1: REMOVE right glove; remaining left glove partially off fingers" |
| Combo | "Edit image 1: Zoom in (scale 1.3x) to Close-up. Subject at door, hand on handle; background reduced" |
| Perspective | "Edit image 1: Shift perspective right by 30 to reveal new details on left; cool side lighting from right" |

**ЗАПРЕЩЕННЫЕ ФОРМУЛИРОВКИ (english_prompt END):**
removing / approaching / moving toward / drifting / then / after that / continues to / 1.0x / 0.99x / 1.01x / "Show character with..." / "Image of..." / "Scene with..." / "no gloves" (без имени персонажа)

**ФОРМАТ ВЫВОДА:**
{{
  "characters": end_technical_params.characters,
  "main_subject": shot_frame_spec.primary_subject,
  "location": end_technical_params.location,
  "final_shot_size": end_technical_params.final_shot_size,
  "final_camera_angle": end_technical_params.final_camera_angle,
  "final_lighting_style": end_technical_params.final_lighting_style,
  "final_color_palette": end_technical_params.final_color_palette,
  "final_camera_position": end_technical_params.final_camera_position,
  "final_character_orientation": end_technical_params.final_character_orientation,
  "final_spatial_composition": end_technical_params.final_spatial_composition,
  "final_point_of_view": end_technical_params.final_point_of_view,
  "spatial_changes_from_start": end_technical_params.spatial_changes_from_start,
  "camera_movement_completed": end_technical_params.camera_movement_completed,
  "composition_stability": end_technical_params.composition_stability,
  "continuity_score": end_technical_params.continuity_score,
  "next_shot_compatibility": end_technical_params.next_shot_compatibility,
  "framing_delta_percent": end_technical_params.framing_delta_percent,
  "subject_scale_ratio": end_technical_params.subject_scale_ratio,
  "prop_continuity": end_technical_params.prop_continuity,
  "english_prompt": "детальный промпт финального стоп-кадра с ЯВНЫМ описанием изменений",
  "negative_prompt": "негативный промпт с учетом континуити",
  "should_link_as_next_start": "true/false",
  "should_use_prev_end_as_reference": "true/false",
  "link_reasoning": "обоснование решения о типе связи"
}}

**КРИТИЧЕСКИ ВАЖНО - ФОРМАТ ОТВЕТА:**
Твой ответ должен быть ТОЛЬКО чистым валидным JSON без каких-либо markdown-блоков, тегов ```json или дополнительного текста.
"""
    )

    # Подготавливаем данные для генерации
    start_summary = {
        "characters": start_llm_result.get("characters", []),
        "english_prompt": start_llm_result.get("english_prompt", ""), # Передаем ПОЛНЫЙ промпт
        "camera_position": start_llm_result.get("camera_position", ""),
        "character_orientation": start_llm_result.get("character_orientation", ""),
        "spatial_composition": start_llm_result.get("spatial_composition", "")
    }
    
    context_for_decisions = {
        "scene_pacing": extended_context.get("scene_pacing", ""),
        "narrative_position": extended_context.get("narrative_position", ""),
        "next_shot": extended_context.get("next_shot", {}),
        "scene_mood": extended_context.get("scene_mood", ""),
        "current_shot_position": extended_context.get("current_shot_position", "")
    }
    shot_character_visual_profiles = (
        extended_context.get("end_shot_character_visual_profiles")
        or extended_context.get("shot_character_visual_profiles")
        or extended_context.get("character_visual_profiles", [])
    )
    
    location_ctx = location_context or {}
    final_size_label = final_shot_size_label or end_technical_params.get("final_shot_size", "")
    location_anchors: List[str] = []
    for raw_anchor in location_ctx.get("key_objects", []) or []:
        anchor = str(raw_anchor or "").strip()
        if not anchor:
            continue
        location_anchors.append(anchor)
        if len(location_anchors) >= 2:
            break
    if not location_anchors:
        fallback_description = str(location_ctx.get("description", "") or "").strip()
        if fallback_description:
            location_anchors.append(fallback_description)

    shot_frame_spec_for_prompt = enrich_shot_frame_spec_environment_delta_via_llm(
        extended_context.get("shot_frame_spec") or {},
        video_prompt=str(video_prompt or ""),
    )

    if isinstance(shot_frame_spec_for_prompt, dict) and shot_frame_spec_for_prompt:
        shot_frame_spec_payload = {
            "primary_subject": shot_frame_spec_for_prompt.get("primary_subject", ""),
            "scene_mode": shot_frame_spec_for_prompt.get("scene_mode", ""),
            "visible_characters": shot_frame_spec_for_prompt.get("visible_characters", []),
            "must_show": shot_frame_spec_for_prompt.get("must_show", []),
            "must_not_show": shot_frame_spec_for_prompt.get("must_not_show", []),
            "visible_readable_texts": shot_frame_spec_for_prompt.get("visible_readable_texts", []),
            "hidden_readable_texts": shot_frame_spec_for_prompt.get("hidden_readable_texts", []),
            "end_state_spec": shot_frame_spec_for_prompt.get("end_state_spec", {}),
            "transition_spec": shot_frame_spec_for_prompt.get("transition_spec", {}),
        }
    else:
        shot_frame_spec_payload = shot_frame_spec_for_prompt
    scene_continuity_facts_for_prompt: Dict[str, Any] = {}

    # Вычисляем явные изменения для LLM
    try:
        end_ratio = float(end_technical_params.get("subject_scale_ratio", 1.0) or 1.0)
    except Exception:
        end_ratio = 1.0
    if end_ratio > RATIO_NO_CHANGE_UPPER:
        ratio_change = f"{end_ratio}x (Zoom in)"
    elif end_ratio < RATIO_NO_CHANGE_LOWER:
        ratio_change = f"{end_ratio}x (Pull back)"
    else:
        ratio_change = "No change"
    start_shot_size = start_summary.get("shot_size", "") or extended_context.get("camera_plan", "")

    _bs_art_end = black_screen_storyboard_shot(
        str(extended_context.get("camera_plan") or ""),
        extended_context.get("full_shot_frame_spec") or extended_context.get("shot_frame_spec"),
    )
    black_screen_end_block = ""
    if _bs_art_end:
        black_screen_end_block = """
**[P0] BLACK SCREEN / ЧЁРНЫЙ ЭКРАН (END):**
- Редактируй только continuity (image 1 от START этого шота): сведи к ровному #000000, визуальный нуль по `shot_frame_spec.end_state_spec`.
- Не добавляй референсы персонажей/локации; `characters`: [].
- Не выдумывай объекты вне must_show (никаких «барханов», смартфонов и т.д., если их нет в данных шота).
"""

    user_prompt = f"""Создай финальный промпт для END кадра, который является МОДИФИКАЦИЕЙ START кадра.
{black_screen_end_block}
LANGUAGE CONTRACT:
- `english_prompt`, `negative_prompt`, `reference_roles_instruction`, `main_subject` и `final_spatial_composition` пиши строго на {prompt_language_label}.
- `video_prompt` оставляй строго на английском языке.
- Для END-редактирования используй `{edit_prefix}`.

{_build_state_to_image_user_core(
    phase_name="END",
    state_spec_name="end_state_spec",
    summary_field="final_spatial_composition",
    composition_field="final_spatial_composition",
    camera_position_field="final_camera_position",
    orientation_field="final_character_orientation",
    shot_size_field_name="final_shot_size",
)}

ДАННЫЕ START КАДРА (БЫЛО):
--------------------------------
Промпт: "{start_summary['english_prompt']}"
Крупность: {start_shot_size}
Позиция камеры: {start_summary['camera_position']}

ДАННЫЕ ФИНАЛА (СТАЛО - ТЕХНИЧЕСКИЙ АНАЛИЗ):
--------------------------------
<end_technical_params>
{json.dumps(end_technical_params, ensure_ascii=False, indent=2)}
</end_technical_params>

АНАЛИЗ ИЗМЕНЕНИЙ ДЛЯ ПРОМПТА:
1. Масштаб: {start_shot_size} -> {final_size_label} (Ratio: {ratio_change})
2. Ракурс: {start_summary['camera_position']} -> {end_technical_params.get('final_camera_position', 'no change')}

ПАСПОРТ ЛОКАЦИИ (КРИТИЧНО ДЛЯ ФОНА):
<location_context>
{json.dumps(location_ctx, ensure_ascii=False, indent=2)}
</location_context>

SHOT FRAME SPEC (авторитетный source of truth для шота; transition_spec.environment_delta дополняется при движении субъекта/камеры):
{json.dumps(shot_frame_spec_payload, ensure_ascii=False, indent=2)}

PER-SHOT CAST VISUAL CANON (ИСПОЛЬЗУЙ ТОЛЬКО ДЛЯ ВИЗУАЛИЗАЦИИ ЭТОГО КАДРА):
{json.dumps(shot_character_visual_profiles, ensure_ascii=False, indent=2)}

ОСНОВНОЙ КОНТЕКСТ:
scene_continuity_facts:
{json.dumps(scene_continuity_facts_for_prompt, ensure_ascii=False, indent=2)}
Описание кадра: {extended_context.get('shot_description', '')}
План камеры: {extended_context.get('camera_plan', '')}

TRANSITION HINT (optional video_prompt; may be empty):
"{video_prompt}"

АНАЛИЗ ИЗМЕНЕНИЙ (КРИТИЧНО - ИСПОЛЬЗУЙ В ПЕРВОЙ ЖЕ КОМАНДЕ):
1. Масштаб: {start_shot_size} -> {final_size_label} (Ratio: {ratio_change})
2. Ракурс: {start_summary['camera_position']} -> {end_technical_params.get('final_camera_position', 'no change')}
3. Пространственные изменения: {end_technical_params.get('spatial_changes_from_start', '')}

ФИНАЛЬНЫЕ ПАРАМЕТРЫ ФИНАЛА:
- Ракурс: {end_technical_params.get('final_camera_angle', '')}
- Позиция: {end_technical_params.get('final_camera_position', '')}
- Точка зрения: {end_technical_params.get('final_point_of_view', '')}
- Ориентация героя: {end_technical_params.get('final_character_orientation', '')}
- Освещение: {end_technical_params.get('final_lighting_style', '')}
- Реквизит: {end_technical_params.get('prop_continuity', {})}
- Пространственные изменения: {end_technical_params.get('spatial_changes_from_start', '')}
- Фоновые якоря локации, которые должны остаться видимыми: {json.dumps(location_anchors, ensure_ascii=False)}

КОНТЕКСТ ДЛЯ ПРИНЯТИЯ РЕШЕНИЙ:
{json.dumps(context_for_decisions, ensure_ascii=False, indent=2)}

ЗАДАЧА:
1. Создай english_prompt КАК ОДНУ КОМАНДУ РЕДАКТИРОВАНИЯ ИЗОБРАЖЕНИЯ:
   - Пиши `english_prompt` и `negative_prompt` строго на {prompt_language_label}; не оставляй англоязычные хвосты вне разрешённых camera/lens терминов и readable texts.
   - `shot_frame_spec` — это source of truth. Сохрани все факты из `must_show`, не добавляй ничего вне spec и не подменяй `primary_subject`.
   - **НАЧНИ С ИНСТРУКЦИИ**: "{edit_prefix} {final_size_label}: [Zoom/Shift command if ratio/yaw changed]. [Action result]. [Atmosphere/Lighting from location_context]."
   - **КРИТИЧНО - ZOOM/SHIFT**: Если ratio ≠ 1.0, используй "Zoom in (scale Xx)" или "Pull back (scale Xx)". Если yaw ≠ 0, используй "Shift perspective [direction] by [degrees]°".
   - **КРИТИЧНО - ИСПОЛЬЗУЙ ОПИСАНИЕ ЛОКАЦИИ ИЗ location_context**: ОБЯЗАТЕЛЬНО используй lighting, color_palette и atmosphere из location_context.
   - **КРИТИЧНО - СОХРАНИ ЯКОРЯ ЛОКАЦИИ**: если `location_anchors` не пуст, явно сохрани в english_prompt хотя бы 1–2 таких фоновых якоря из того же места.
     Для close-up это должны быть частичные, но узнаваемые фрагменты вокруг героя, а не новый абстрактный фон.
   - **КРИТИЧНО - CONTINUITY GEOMETRY FIRST**: если END редактирует `image 1` как continuity reference, сохрани идентичность локации (материалы, стиль, топология среды), но при **любом** относительном движении субъекта или камеры **не** требуй пиксельно того же фона, что в START: видимое окружение должно обновиться (параллакс / сдвиг деталей / боке), как в `transition_spec.environment_delta`.
   - **КРИТИЧНО — ОКРУЖЕНИЕ ПРИ ДВИЖЕНИИ**: если в `transition_spec.environment_delta` или в `video_prompt`/техническом контексте есть движение субъекта (жест, шаг, наклон, бег, …) и/или движение камеры (трекинг, наезд, панорама, кран, …) — в `english_prompt` ОБЯЗАТЕЛЬНО опиши **изменение видимого окружения** относительно START: в широком/среднем плане — сдвиг фона/архитектурных деталей; в крупном — сдвиг боке/световых пятен/отражений. Та же локация, не студийный/абстрактный задник. Без новых объектов вне shot_description.
   - **КРИТИЧНО - НЕ ДЕРЖИ СТАРУЮ КОМПОЗИЦИЮ ИЗ CONTINUITY**: если END делает tighter crop / zoom-in по сравнению со START, continuity reference должен сохранять identity, pose lineage, lighting, props and environment continuity, но НЕ старую full-frame composition.
     В `reference_roles_instruction` не описывай image 1 как источник "composition", если финальное кадрирование заметно теснее START.
   - **КРИТИЧНО - ПОДЧИНИ КАДРИРОВАНИЕ ФИНАЛЬНОЙ КРУПНОСТИ**: если `final_shot_size` = Close-up / Medium close-up, сделай героя визуально крупнее и плотнее в кадре;
     не описывай полный рост, если storyboard.description явно этого не требует.
   - **КРИТИЧНО - PHYSICAL RELATION LOCK**: если в END должен быть одновременно читаем герой и видимый край опоры / люка / разрыва поверхности / точки контакта,
     держи этот узел в одном кадре. Не смещай героя в сторону только для того, чтобы показать отверстие отдельно, и не перекрывай саму причинную зону чрезмерным кропом.
     Если tighter crop ломает эту связь, делай кадр чуть шире, но сохраняй тот же continuity background.
   - **КРИТИЧНО - SINGLE FRAME COMPOSITION**: если в close-up остаётся видимый краевой фрагмент среды/опоры/разрыва, показывай его как часть того же изображения, а НЕ как отдельную панель, вставку, cutaway strip или split composition.
   - **КРИТИЧНО - WORLD PHYSICS**: если в `shot_frame_spec` или `end_state_spec` есть `world_physics`, переведи его в визуально читаемое состояние мира: опора, поверхность, устойчивость, контакты, зазоры, перекрытия и запрещённые физические трактовки.
     Не замещай это частными edge-case формулировками; выводи финальный кадр из `world_physics`.
   - **ВИЗУАЛИЗАЦИЯ**: описывай героя только через прямые видимые признаки из PER-SHOT CAST VISUAL CANON. Используй лицо, глаза, пропорции, прическу, одежду, аксессуары и устойчивые видимые признаки. Не тащи в prompt роль, профессию, биографию, характер или речевые паттерны.
   - **MULTI-CHARACTER LOCK**: если в PER-SHOT CAST VISUAL CANON несколько персонажей, сохраняй их как отдельные визуальные сущности по `shot_role`; не смешивай одежду, лицо, аксессуары и силуэт между ними.
   - Если у персонажа в PER-SHOT CAST VISUAL CANON есть `phase_pose_signature`, используй её как authoritative позу именно этого персонажа в END.
   - **КРИТИЧНО - ОРИЕНТАЦИЯ ПЕРСОНАЖЕЙ (ОБЯЗАТЕЛЬНО ДЛЯ END):** 
     * Если `final_character_orientation` = "facing_each_other" → ПЕРСОНАЖИ СМОТРЯТ ДРУГ НА ДРУГА, НЕ В КАМЕРУ!
     * Явно укажи в промпте: "[Персонаж A] смотрит на [Персонаж B]", "их лица обращены друг к другу", "facing each other"
     * НЕ используй формулировки типа "смотрят в камеру", "looking at camera", "facing camera" если `final_character_orientation` = "facing_each_other"
     * Если `final_character_orientation` = "facing_camera" → тогда можно "смотрит в камеру", но только при явном direct-address / frontal-lens framing
     * Если `final_character_orientation` = "facing_away" → "смотрит от камеры", "спиной к камере"
     * Если `final_character_orientation` = "profile" → "в профиль", "side view"
     * Если `shot_description` говорит "лицо в сантиметре от его лица" / "лица почти касаются" → это ОБЯЗАТЕЛЬНО означает, что они смотрят друг на друга!
     * ЦЕЛЬ: обеспечить правильную ориентацию персонажей, чтобы они смотрели друг на друга, когда это требуется shot_description
   - **BACKGROUND & EXTRAS LOCK (без заморозки кадра)**: сохраняй ту же локацию и узнаваемые якоря, но при любом движении субъекта/камеры видимое окружение **должно** отличаться от START там, где это следует из `environment_delta`/video_prompt; не копируй фон 1:1 из Image 1, если ожидается сдвиг. Не подменяй локацию на студию/градиент/небо.
   - **ЗАПРЕЩЕНО ПОДМЕНЯТЬ ФОН**: не заменяй continuity/location background на пустое небо, студийную стену, безымянную тёмную плоскость или generic gradient backdrop.
   - **NO OVERLAY TEXT**: если в `shot_frame_spec.visible_readable_texts` нет авторизованного readable text, явно удерживай кадр как unlabeled illustration: no captions, no title cards, no subtitles, no name labels, no credits, no lower-third text.
   - **РЕКВИЗИТ**: Отрази все изменения из prop_continuity (REMOVE/ADD/TRANSFORM).
   - **РЕЗУЛЬТАТ**: Описывай состояние ПОСЛЕ действия из shot_description/end_technical_params, а не процесс.
   - **[P1 CRITICAL] GROUNDING**: Всегда указывай физическую grounding через `world_physics`, а если её нет — через минимально необходимую опору и якорь из location_context.

ПРИМЕРЫ КОМАНД:
- Dolly In (Scale): "Edit image 1: Zoom in (scale 1.25x) to Close-up. Subject fills more of frame, background details tighter; maintain lighting"
- Perspective Shift: "Edit image 1: Shift perspective to the right by 30° to reveal new background details; maintain character position"
- Character Action: "Edit image 1: TRANSFORM subject pose: hand now grasping the object firmly; maintain atmosphere"

2. Создай negative_prompt с учетом континуити.
   - Если `location_anchors` не пуст, negative_prompt должен запрещать blank/abstract/studio-like background replacement for this END frame.
3. Определи тип связи (should_link_as_next_start / should_use_prev_end_as_reference).

**ФОРМАТ ВЫВОДА:**
{{
  "main_subject": "точный главный субъект кадра; по умолчанию = shot_frame_spec.primary_subject",
  "camera_position": "перенеси из technical params, если не требуется иное",
  "character_orientation": "перенеси из technical params, если не требуется иное",
  "spatial_composition": "непустая композиция кадра, согласованная с technical params и shot_frame_spec.must_show",
  "point_of_view": "objective / pov / etc",
  "initial_state_summary": "непустое статичное T=0 summary без процесса и без последовательности",
  "english_prompt": "команда редактирования START T=0",
  "negative_prompt": "запреты по типу кадра",
  "reference_image_paths": ["путь1", "путь2"],
  "reference_roles_instruction": "image 1 as...; image 2 as...",
  "characters": ["имя1", "имя2"],
  "prop_continuity": {{"removed": [], "kept": [], "added": []}},
  "should_link_as_next_start": "true",
  "should_use_prev_end_as_reference": "false",
  "link_reasoning": "short reason"
}}

Обязательно начни english_prompt с префикса "{edit_prefix}" и затем укажи финальную крупность плана: {final_size_label}
"""

    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_ultimate,
            max_tokens=12000,
            temperature=0.4,
            response_format={"type": "json_object"},
        )
        
        # Логируем исходный ответ для диагностики
        if response:
            logger.debug(f"🔍 Исходный ответ модели для END (первые 500 символов): {response[:500]}")
        else:
            logger.warning("⚠️ Модель вернула пустой ответ для END")
        
        result = parse_llm_json(response)
        if not isinstance(result, dict):
            logger.error(f"❌ END художественная генерация вернула {type(result).__name__} вместо dict: {str(result)[:200]}")
            return None
        return result

    except json.JSONDecodeError as e:
        logger.error(f"❌ Ошибка парсинга JSON в художественной генерации END кадра: {e}")
        logger.error(f"📄 Исходный текст ответа:\n{response if 'response' in locals() else 'Ответ не получен'}")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка художественной генерации END кадра: {e}")
        if 'response' in locals():
            logger.error(f"📄 Исходный текст ответа:\n{response}")
        return None

def _convert_end_to_start_shot_item(
    previous_end_shot_item: Dict[str, Any],
    previous_end_llm_result: Dict[str, Any],
    project_id: str,
    scene_number: int,
    shot_number: int,
    page_number: int,
    item_number: int,
    camera_plan: str,
    timing: str,
    characters_data: List[Dict[str, Any]],
    locations_data: List[Dict[str, Any]],
    scene_action: str,
    shot_description: str,
    scene_characters: List[str],
    scene_continuity_facts: Optional[Dict[str, Any]] = None,
    location_time: str = "",
    language: str = "en",
    seed: Optional[int] = None
) -> Dict[str, Any]:
    """
    Конвертирует end кадр предыдущего shot'а в start кадр текущего.
    Генерирует новый полноценный start кадр с video_prompt.
    """
    
    logger.info(f"🎬 Генерируем новый start кадр на основе предыдущего end кадра")
    linked_location_canon_name = ""
    try:
        prev_locs = previous_end_shot_item.get("locations") or []
        if prev_locs and isinstance(prev_locs[0], dict):
            linked_location_canon_name = str(prev_locs[0].get("name") or "").strip()
    except Exception:
        linked_location_canon_name = ""

    def _map_camera_plan_to_shot_size(plan: str) -> str:
        p = (plan or "").lower()
        if "круп" in p or "close" in p:
            return "Close-up"
        if "сред" in p or "medium" in p:
            return "Medium shot"
        if "общ" in p or "wide" in p:
            return "Wide shot"
        return ""
    
    # Подготавливаем референсы для LLM (включая continuity reference)
    enhanced_characters_data = list(characters_data)
    enhanced_locations_data = list(locations_data)
    
    # Добавляем continuity reference как специальный "персонаж"
    continuity_ref = {
        "name": "Continuity Reference",
        "reference_image_path": previous_end_shot_item["output_path"],
        "role": "Continuity reference from previous shot",
        "type": "continuity"
    }
    enhanced_characters_data.insert(0, continuity_ref)  # Первым в списке
    
    # Строим расширенный контекст с continuity reference
    # Подмешиваем style_images.json напрямую (для linked start у нас screenplay_data минимальный)
    style_images_data = {}
    try:
        style_images_path = f"plots/storybooks/{project_id}/30_style/style_images.json"
        if os.path.exists(style_images_path):
            with open(style_images_path, "r", encoding="utf-8") as f:
                style_images_data = json.load(f) or {}
    except Exception:
        style_images_data = {}

    extended_context = _build_extended_context(
        project_id=project_id,
        scene={
            "scene_number": scene_number,
            "action": scene_action,
            "characters": scene_characters,
            "location_time": location_time or "",
            "location_canon_name": linked_location_canon_name or "",
        },
        storyboard=[{"shot_number": shot_number, "description": shot_description, "camera_plan": camera_plan}],
        shot_number=shot_number,
        scene_action=scene_action,
        shot_description=shot_description,
        camera_plan=camera_plan,
        scene_characters=scene_characters,
        screenplay_data={},  # Минимальные данные для совместимости
        characters_data=enhanced_characters_data,  # С continuity reference
        locations_data=enhanced_locations_data,
        scene_continuity_facts=scene_continuity_facts,
        continuity_reference_path=previous_end_shot_item["output_path"],  # Дополнительная информация
        style_images=style_images_data
    )
    
    new_start_llm_result = _generate_shot_prompt(
        extended_context=extended_context,
        shot_type="start",
        language=language,
    )

    # ------------------------------------------------------------
    # POV BREAK DETECTOR:
    # Если следующий кадр требует СМЕНЫ ТОЧКИ ЗРЕНИЯ относительно главного персонажа
    # (например, previous END был "behind", а новый START требует "in_front"),
    # то НЕ используем предыдущий END как image 1 continuity reference.
    # Иначе модель часто "сохраняет" геометрию/ракурс, и POV не меняется.
    # ------------------------------------------------------------
    def _norm_pos(v: Any) -> str:
        s = str(v or "").strip().lower()
        if not s:
            return ""
        # Нормализация распространённых синонимов/вариантов
        s = s.replace("-", "_").replace(" ", "_")
        aliases = {
            "front": "in_front",
            "infront": "in_front",
            "in_front_of": "in_front",
            "facing_front": "in_front",
            "back": "behind",
            "rear": "behind",
            "from_behind": "behind",
            "over_shoulder": "behind",
            "over_the_shoulder": "behind",
            "ots": "behind",
            "profile": "side",
            "three_quarter": "side",
            "left": "side",
            "right": "side",
            "top": "above",
            "birdseye": "above",
            "bird_eye": "above",
            "overhead": "above",
            "high": "above",
            "low": "below",
            "underside": "below",
            "under": "below",
        }
        return aliases.get(s, s)

    def _is_pov_break(prev_pos: str, next_pos: str) -> bool:
        """
        Существенная смена POV: любой переход между различными camera_position
        (after normalization), например behind→in_front, side→in_front, above→side и т.д.
        """
        if not prev_pos or not next_pos:
            return False
        return prev_pos != next_pos

    pov_break_detected = False
    try:
        _prev = (previous_end_llm_result or {})
        _next = (new_start_llm_result or {})
        # Defensive: LLM может вернуть non-string для camera_position
        _prev_raw = _prev.get("final_camera_position") or _prev.get("camera_position")
        _next_raw = _next.get("camera_position")
        prev_pos = _norm_pos(_prev_raw if isinstance(_prev_raw, str) else "")
        next_pos = _norm_pos(_next_raw if isinstance(_next_raw, str) else "")
        if _is_pov_break(prev_pos, next_pos):
            logger.info(
                f"🎥 POV BREAK: previous_end camera_position='{prev_pos}', next_start camera_position='{next_pos}'. "
                f"Generating independent START without continuity image 1."
            )
            pov_break_detected = True
            # Строим независимый контекст (без continuity_reference_path и без continuity_ref в characters_data)
            extended_context_independent = _build_extended_context(
                project_id=project_id,
                scene={
                    "scene_number": scene_number,
                    "action": scene_action,
                    "characters": scene_characters,
                    "location_time": location_time or "",
                    "location_canon_name": linked_location_canon_name or "",
                },
                storyboard=[{"shot_number": shot_number, "description": shot_description, "camera_plan": camera_plan}],
                shot_number=shot_number,
                scene_action=scene_action,
                shot_description=shot_description,
                camera_plan=camera_plan,
                scene_characters=scene_characters,
                screenplay_data={},
                characters_data=list(characters_data),
                locations_data=list(locations_data),
                scene_continuity_facts=scene_continuity_facts,
                continuity_reference_path=None,
                style_images=style_images_data
            )
            independent_start = _generate_shot_prompt(
                extended_context=extended_context_independent,
                shot_type="start",
                language=language,
            )
            if independent_start:
                new_start_llm_result = independent_start
                # Также не используем enhanced_characters_data дальше
                enhanced_characters_data = list(characters_data)
                enhanced_locations_data = list(locations_data)
    except Exception as e:
        logger.warning(f"⚠️ POV BREAK detector failed: {e}")
    
    if not new_start_llm_result:
        logger.error(f"❌ Не удалось сгенерировать LLM результат для linked start кадра")
        # Fallback на старую логику с previous_end_llm_result
        llm_result_to_use = previous_end_llm_result
    else:
        # Используем новый START, не копируя финальный текст END (чтобы не тащить конечные формулировки)
        llm_result_to_use = new_start_llm_result
    
    # Ранее здесь выполнялась пост-обработка START; теперь управление чисто через LLM правила
    
    # Создаем shot_item с новыми данными (включая continuity reference)
    # Если был POV break — создаём независимый START: не маркируем как linked start и не тащим previous_end как image 1.
    # Также защищаемся от случаев, когда модель вернула reference_image_paths с /97_shots/ по инерции.
    llm_result_to_use_for_item = llm_result_to_use
    if pov_break_detected and isinstance(llm_result_to_use_for_item, dict):
        refs = llm_result_to_use_for_item.get("reference_image_paths")
        if isinstance(refs, list) and refs and isinstance(refs[0], str) and ("/97_shots/" in refs[0]):
            llm_result_to_use_for_item = {**llm_result_to_use_for_item, "reference_image_paths": refs[1:]}

    new_shot_item = _create_shot_item(
        project_id=project_id,
        scene_number=scene_number,
        shot_number=shot_number,
        shot_type="start",
        page_number=page_number,
        item_number=item_number,
        camera_plan=camera_plan,
        timing=timing,
        llm_result=llm_result_to_use_for_item,
        characters_data=enhanced_characters_data,  # С continuity reference
        locations_data=enhanced_locations_data,
        location_time=location_time,
        location_canon_name=extended_context.get("location_canon_name", ""),
        scene_action=scene_action,
        shot_description=shot_description,
        shot_frame_spec=extended_context.get("full_shot_frame_spec") or extended_context.get("shot_frame_spec"),
        shot_frame_spec_cache_key=extended_context.get("shot_frame_spec_cache_key", ""),
        scene_continuity_facts=extended_context.get("scene_continuity_facts"),
        language=language,
        is_linked_start=not pov_break_detected,  # При POV break это независимый старт без image 1
        seed=seed,  # Linked shots don't need seed consistency
        visual_style=str(extended_context.get("visual_style") or ""),
        style_do_not_include=extended_context.get("style_do_not_include"),
    )
    
    # Решение о типе связи: учитываем семантическое решение LLM о link_type
    end_final_size = (previous_end_llm_result or {}).get("final_shot_size", "")
    planned_start_size = _map_camera_plan_to_shot_size(camera_plan)
    
    # НОВАЯ ЛОГИКА: сначала проверяем семантику link_type из LLM решения
    llm_link_type = (previous_end_llm_result or {}).get("link_type", "").strip().lower()
    llm_should_link = str((previous_end_llm_result or {}).get("should_link_as_next_start", "false")).strip().lower() == "true"
    
    # Если LLM явно указал "independent" - НЕ копируем, независимо от числовых критериев
    if pov_break_detected:
        # POV break всегда запрещает full_copy и использование previous_end как базы.
        strict_link = False
    elif llm_link_type == "independent":
        logger.info(f"🔗 LLM указал link_type='independent' - принудительно отключаем копирование")
        strict_link = False
    else:
        # Используем LLM решение о should_link_as_next_start
        strict_link = llm_should_link
        
    # Camera-plan compatibility guard:
    # If previous_end has no reliable final_shot_size, falling back to camera_plan prevents accidental full_copy
    # across radically different shot types (e.g., Extreme Close-Up eye → Medium shot action).
    def _camera_plan_signature(cp: str) -> str:
        s = (cp or "").strip().lower()
        # POV-признак определяется по camera_plan строке; сама shot-size family берётся
        # только из канонического маппера (_map_camera_plan_to_shot_size).
        # Если маппер не вернул значение — сигнатура остаётся без fam, и внешний guard
        # (camera_plan_compatible) корректно фиксирует это как "неизвестный план".
        is_pov = "pov" in s or "point of view" in s or "от первого лица" in s
        fam = (_map_camera_plan_to_shot_size(cp) or "").strip().lower()
        return ("pov:" if is_pov else "obj:") + fam

    prev_cp = str((previous_end_shot_item or {}).get("camera_plan", "") or "")
    next_cp = str(camera_plan or "")
    camera_plan_compatible = (_camera_plan_signature(prev_cp) == _camera_plan_signature(next_cp))

    # If end_final_size missing, DO NOT treat it as automatically compatible; use camera_plan compatibility.
    if not str(end_final_size or "").strip():
        size_compatible = camera_plan_compatible
    else:
        size_compatible = (planned_start_size == "" or planned_start_size.lower() == str(end_final_size).strip().lower())

    if strict_link and size_compatible:
        # Полное копирование допустимо
        logger.info(f"🔗 Применяется полное копирование: strict_link={strict_link}, size_compatible={size_compatible}")
        new_shot_item.update({
            "copy_from_previous_end": True,  # Используется в artist_batch_edit.py
            "source_end_path": previous_end_shot_item["output_path"],
            "image_path": None,
            "link_type": "full_copy",
            "should_link_as_next_start": True,
            "link_reasoning": f"Полное копирование END кадра как START следующего кадра (LLM link_type: {llm_link_type}, should_link: {llm_should_link})"
        })
    else:
        # Мягкая связь: LLM уже получил continuity reference через enhanced_characters_data
        reason_parts = []
        if llm_link_type == "independent":
            reason_parts.append("LLM указал независимый тип связи")
        if not strict_link:
            reason_parts.append("LLM не рекомендует строгую связь")
        if not size_compatible:
            reason_parts.append("несовместимые размеры кадров")
        
        reasoning = "Использование предыдущего END как референса: " + "; ".join(reason_parts)
        logger.info(f"🔗 Применяется референсная связь: {reasoning}")
        
        new_shot_item.update({
            "copy_from_previous_end": False,
            "link_type": "reference", 
            "should_link_as_next_start": False,
            "should_use_prev_end_as_reference": (not pov_break_detected),
            "link_reasoning": reasoning
        })
    
    return new_shot_item
