"""
Модуль технических функций для генерации кадров
"""

from typing import Dict, Any, Optional, List
import json
import re
import os
from agent_command import model_hard, model_code, model_ultimate, model_lite
from utils import call_openai_api, extract_json_from_markdown
import logging
from .shared_utils import (
    _build_extended_context,
    _create_missing_location_llm,
    _validate_negative_prompt_consistency,
    black_screen_storyboard_shot,
    merge_style_do_not_into_negative,
)

logger = logging.getLogger(__name__)


def _character_reference_should_be_primary_for_img2img(
    *,
    shot_frame_spec: Optional[Dict[str, Any]],
    shot_type: str,
    continuity_ref: Optional[str],
    char_refs_in_order: List[str],
    location_ref: Optional[str],
    shot_characters: List[Dict[str, Any]],
) -> bool:
    """
    Если True — первый вход img2img API (image 1) должен быть референсом персонажа, а не локации.

    Решение только из shot_frame_spec.scene_mode и согласованности primary_subject с персонажами
    (без эвристик по тексту промпта). При continuity reference порядок не меняем: image 1 остаётся continuity.
    """
    if continuity_ref:
        return False
    if str(shot_type or "").strip().lower() != "start":
        return False
    if not char_refs_in_order or not location_ref:
        return False

    spec = shot_frame_spec if isinstance(shot_frame_spec, dict) else {}
    mode = str(spec.get("scene_mode") or "").strip().lower()
    if mode == "environment":
        return False
    if mode in ("single_subject", "ensemble"):
        return True
    if mode == "object_focus":
        primary = str(spec.get("primary_subject") or "").strip().casefold()
        if not primary:
            return False
        visible = spec.get("visible_characters")
        if isinstance(visible, list):
            for entry in visible:
                name = str(entry or "").strip().casefold()
                if name and name == primary:
                    return True
        for c in shot_characters:
            cn = str(c.get("name") or "").strip().casefold()
            if cn and cn == primary:
                return True
        return False
    return False


def _infer_accessory_visibility_via_llm(
    *,
    shot_type: str,
    shot_frame_spec: Optional[Dict[str, Any]],
    llm_result: Dict[str, Any],
    shot_characters: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """
    Через model_lite определяет, какие аксессуары/элементы одежды должны быть видимы/скрыты
    в конкретном кадре с учетом ракурса, ориентации и окклюзий.
    """
    visual_items: List[Dict[str, Any]] = []
    for ch in shot_characters or []:
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("name") or "").strip()
        variable = ch.get("variable_attributes") if isinstance(ch.get("variable_attributes"), dict) else {}
        accessories = [str(x).strip() for x in (variable.get("accessories") or []) if str(x).strip()]
        base_clothing = str(variable.get("base_clothing") or "").strip()
        items = []
        if base_clothing:
            items.append(base_clothing)
        items.extend(accessories)
        if not name or not items:
            continue
        visual_items.append({"character": name, "items": items[:8]})

    if not visual_items:
        return {"must_show_additions": [], "must_not_show_additions": [], "negative_prompt_tokens": []}

    spec = shot_frame_spec if isinstance(shot_frame_spec, dict) else {}
    payload = {
        "shot_type": shot_type,
        "camera_position": str(llm_result.get("camera_position") or llm_result.get("final_camera_position") or "").strip(),
        "character_orientation": str(llm_result.get("character_orientation") or llm_result.get("final_character_orientation") or "").strip(),
        "point_of_view": str(llm_result.get("point_of_view") or llm_result.get("final_point_of_view") or "").strip(),
        "spatial_composition": str(llm_result.get("spatial_composition") or llm_result.get("final_spatial_composition") or "").strip(),
        "shot_frame_spec": {
            "primary_subject": spec.get("primary_subject"),
            "visible_characters": spec.get("visible_characters"),
            "must_show": spec.get("must_show"),
            "must_not_show": spec.get("must_not_show"),
            "world_physics": spec.get("world_physics"),
        },
        "character_visual_items": visual_items,
    }
    system_prompt = (
        "Ты валидатор видимости аксессуаров/элементов одежды в одном storyboard-кадре.\n"
        "Определи, какие предметы обязаны быть видимы, а какие должны быть скрыты/окклюдированы\n"
        "из-за ракурса/ориентации/кадрирования/физики (например, chest-бейдж не виден при back view).\n"
        "Не придумывай новые предметы.\n"
        "Верни только JSON:\n"
        "{\"must_show_additions\": [\"...\"], \"must_not_show_additions\": [\"...\"], \"negative_prompt_tokens\": [\"...\"]}"
    )
    try:
        resp = call_openai_api(
            prompt="INPUT:\n" + json.dumps(payload, ensure_ascii=False),
            system_prompt=system_prompt,
            model=model_lite,
            max_tokens=500,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        obj = json.loads(extract_json_from_markdown(resp))
        must_show = [str(x).strip() for x in (obj.get("must_show_additions") or []) if str(x).strip()]
        must_not = [str(x).strip() for x in (obj.get("must_not_show_additions") or []) if str(x).strip()]
        neg_tokens = [str(x).strip() for x in (obj.get("negative_prompt_tokens") or []) if str(x).strip()]
        return {
            "must_show_additions": must_show,
            "must_not_show_additions": must_not,
            "negative_prompt_tokens": neg_tokens,
        }
    except Exception as e:
        logger.warning("accessory visibility LLM pass failed: %s", e)
        return {"must_show_additions": [], "must_not_show_additions": [], "negative_prompt_tokens": []}


def _analyze_shot_technical(
    extended_context: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Первый этап: технический анализ кадра.
    Определяет технические параметры съемки и участвующие элементы.
    """
    
    system_prompt = """
═══════════════════════════════════════════════════════════════
LAYER A — ROLE + CONTRACT
═══════════════════════════════════════════════════════════════
Ты -- оператор-постановщик. Технический анализ START кадра (T=0).

[P0] SHOT_FRAME_SPEC = SOURCE OF TRUTH:
- shot_frame_spec -- выбранный source of truth для шота.
- Обязательные поля: primary_subject, visible_characters, must_show, must_not_show, camera_anchor.
- pose_signature (из shot_frame_spec или start_state_spec) обязательна: initial_state_summary, character_actions и композиция сохраняют жестовый рисунок.
- character_pose_signatures из start_state_spec -- authoritative позы; не своди второстепенных к generic massing.
- scene_action/scene_continuity_facts шире шота -> следуй shot_frame_spec. Нельзя подменять шот другим событием сцены.

[P0] STORYBOARD LOCKS:
- Приоритет: shot_frame_spec > shot_description > scene_action (исключение: устойчивые факты сцены).
- Анализируй ТОЛЬКО текущий кадр. T=0 = статичное состояние БЕЗ намерений и процессов.
- scene_continuity_facts (гардероб/предметы/элементы): используй если не противоречат shot_description и уместны по крупности (Wide/Medium -- включай; Close-up -- только если попадает в кадр). НЕ добавляй токсичность/мат/искажение имен.

═══════════════════════════════════════════════════════════════
LAYER B -- STEP-BY-STEP ALGORITHM
═══════════════════════════════════════════════════════════════

STEP 1 -- CHARACTERS:
1a. Первичный список: shot_frame_spec.visible_characters.
1b. Проверь shot_description: включи персонажа если упомянут ЯВНО по имени ИЛИ косвенно ("его глаза" -> определи кто).
1c. НЕ включай персонажей из scene_characters, отсутствующих в shot_description.
1d. Имена -- ТОЧНОЕ написание из available_characters (кириллица, регистр).
1e. REACTION SHOT (camera_plan = "REACTION SHOT"): реагирующий персонаж первым; spatial_composition фокус на нем.

STEP 2 -- LOCATION:
2a. ОБЯЗАТЕЛЬНО выбери из available_locations. НЕ создавай новые.
2b. Если shot_description ЯВНО указывает локацию -> она приоритетнее location_canon_name.
2c. Запрещено: "ИНТ/ЭКСТ", время суток, слэши, "комбинированные" названия.
2d. Если location_time содержит несколько зон, но shot_description указывает конкретную -> используй её.

STEP 3 -- SHOT SIZE / CAMERA:
Выбери параметры из таблиц Layer C. Учитывай camera_plan, shot_description, scene_pacing.

STEP 4 -- SPATIAL COMPOSITION:
4a. camera_position: behind/in_front/side/above/below (относительно главного персонажа).
4b. character_orientation: facing_camera/facing_away/profile/three_quarter.
4c. point_of_view: objective/subjective/POV_[имя].
4d. spatial_composition: position (left/center/right) + depth (fore/mid/back) + subject_dominance (low/medium/high) + background_dominance. Формулируй КАК КОМАНДЫ РЕДАКТИРОВАНИЯ.
Формат: "subject center midground with medium subject dominance; industrial machinery background with low dominance; eye-level framing from front; objective view"
4e. FACE/GAZE: если лицо читается -- фиксируй выражение и взгляд (выводи из shot_description/camera_plan/контекста, НЕ default к "neutral"/"blank"/"looking at camera"). При внезапном физическом сдвиге -- раннее напряжение, НЕ пустота.
4f. facing_camera допустим ТОЛЬКО если shot_description/camera_plan явно требуют прямого взгляда в объектив. Иначе -> three_quarter или profile.

STEP 5 -- INITIAL STATE SUMMARY (T=0):
5a. Действие/изменение в shot_description -> T=0 = ИСХОДНАЯ ПОЗИЦИЯ (замах, группировка, старт), НЕ результат.
5b. ЗАПРЕТ КОНТАКТА: субъект и объект НЕ касаются если действие = "схватить/ударить".
5c. Трансформация окружения (пол проваливается, мост рушится, дверь открывается) -> T=0 = состояние ДО трансформации. Поверхность ЦЕЛОСТНАЯ и СТАБИЛЬНАЯ. ЗАПРЕЩЕНО: "на пороге", "первые признаки", "начинающиеся трещины", "imminent", "threshold of". Если цела — просто ЦЕЛА.
5d. Резкий перевод камеры (snap zoom/whip pan) -> T=0 = начало шота, НЕ финальный кадр.
5e. Если читается поза/жест -- фиксируй видимую геометрию тела, НЕ generic standing/resting.

STEP 6 -- LOCATION CONTEXT:
6a. Первый кадр сцены ИЛИ локация изменилась -> СОЗДАЙ новый location_context.
6b. Локация повторяется -> КОПИРУЙ key_features из previous_shot ТОЧНО, БЕЗ ПЕРЕФОРМУЛИРОВОК. НЕ добавляй новые объекты.
6c. key_features: 3-5 ЕДИНЫХ объектов (используй "with/embedded/integrated", НЕ разделяй на части).

═══════════════════════════════════════════════════════════════
LAYER C -- REFERENCE TABLES
═══════════════════════════════════════════════════════════════

CAMERA PARAMS (допустимые значения):
  shot_size       : Close-up | Medium shot | Wide shot | Extreme close-up
  camera_angle    : Eye-level | High-angle | Low-angle | Dutch angle
  lighting_style  : High-key | Low-key | Rembrandt | Side light  (БЕЗ warm/cool)
  color_palette   : warm | cold | monochrome  (отдельно от lighting_style)
  camera_movement : Static | Pan | Tilt | Dolly | Tracking | Crane

SPATIAL COMPOSITION FORMAT:
  camera_position        : behind | in_front | side | above | below
  character_orientation  : facing_camera | facing_away | profile | three_quarter
  point_of_view          : objective | subjective | POV_[имя]

T=0 TRANSLATION PATTERNS:
  shot_description             | T=0 output
  "Бьет кулаком"              | "fist pulled back near shoulder"
  "Прыгает"                   | "crouched low ready to spring"
  "Язык хватает муху"         | "mouth open, tongue inside; fly airborne nearby"
  "Тянется к чашке"           | "hand near body, gaze on cup"
  "Пол -> люк-ловушка"        | "standing on plate, first hairline crack at edges"
  "Дверь распахивается"       | "door barely ajar, just beginning to open"
  T=0 static templates        | "standing motionless" / "static pose with" / "frozen in mid-action" / "in pre-action posture"

LINGUISTIC MAPPING (Russian -> camera):
  "смотрит в окно/на объект"  | camera_position: side/behind; orientation: facing_away/profile
  "через плечо"               | camera_position: behind; orientation: facing_away; объект впереди
  "лицом к [цели]"            | ориентация к цели; facing_camera ТОЛЬКО если цель = камера/зритель

FORBIDDEN IN T=0 (absolute ban):
  Future intent    : "about to" / "begins to" / "starts to" / "going to" / "will" / "is starting"
  Motion verbs     : "walking" / "running" / "moving" / "approaching" / "advancing" / "retreating"
  Intent phrases   : "preparing to" / "intends to" / "set to"
  Completion verbs : "holding" / "touching" / "hitting" (для целевого действия)
  Completed transforms : результат трансформации окружения, относящийся к более позднему моменту шота

CONTEXT FACTORS (P3):
  - location_time -> освещение; scene_mood -> color_palette; scene_pacing -> camera_movement
  - Соседние кадры -> преемственность (НЕ копируй их действия!)

JSON OUTPUT FORMAT:
{
  "characters": ["имя1", "имя2"],
  "location": "из available_locations (ОБЯЗАТЕЛЬНОЕ ПОЛЕ)",
  "shot_size": "...", "camera_angle": "...", "lighting_style": "...",
  "color_palette": "...", "camera_movement": "...",
  "camera_position": "behind/in_front/side/above/below",
  "character_orientation": "facing_camera/facing_away/profile/three_quarter",
  "spatial_composition": "position + depth + dominance string",
  "point_of_view": "objective/subjective/POV_name",
  "character_actions": "видимая геометрия действий в T=0",
  "environmental_changes": "изменения окружения/освещения",
  "initial_state_summary": "T=0 состояние (без движения и намерений)",
  "location_context": {
    "name": "краткое_имя",
    "key_features": ["unified_element1", "unified_element2", "unified_element3"],
    "atmosphere": "краткое_описание",
    "dominant_colors": ["цвет1", "цвет2"]
  }
}"""

    user_prompt = f"""Проведи технический анализ кадра с учетом полного контекста:

ОСНОВНЫЕ ДАННЫЕ КАДРА:
Действие сцены: {extended_context.get('scene_action', '')}
--------------------------------
Описание кадра: {extended_context.get('shot_description', '')}
--------------------------------
План камеры: {extended_context.get('camera_plan', '')}
--------------------------------
SHOT FRAME SPEC (авторитетный source of truth):
{json.dumps(extended_context.get('shot_frame_spec', {}), ensure_ascii=False, indent=2)}
--------------------------------
Персонажи в сцене: {extended_context.get('scene_characters', [])}
--------------------------------

РАСШИРЕННЫЙ КОНТЕКСТ СЦЕНЫ:
Время и место: {extended_context.get('location_time', '')}
--------------------------------
Звуковое сопровождение: {extended_context.get('scene_sound', '')}
--------------------------------
Режиссерские заметки по камере: {extended_context.get('scene_camera_notes', '')}
--------------------------------
Настроение сцены: {extended_context.get('scene_mood', '')}
--------------------------------
Контекст освещения: {extended_context.get('lighting_context', '')}
--------------------------------
Темпоритм сцены: {extended_context.get('scene_pacing', '')}
--------------------------------
Позиция в повествовании: {extended_context.get('narrative_position', '')}
--------------------------------
Устойчивые факты сцены (scene_continuity_facts):
{json.dumps(extended_context.get('scene_continuity_facts', {}), ensure_ascii=False)}
--------------------------------

КОНТЕКСТ СОСЕДНИХ КАДРОВ:
--------------------------------
Предыдущий кадр: {json.dumps(extended_context.get('previous_shot', {}), ensure_ascii=False)}
--------------------------------
Следующий кадр: {json.dumps(extended_context.get('next_shot', {}), ensure_ascii=False)}
--------------------------------
Позиция кадра: {extended_context.get('current_shot_position', '')}
--------------------------------

СПРАВОЧНИКИ:
Доступные персонажи: {extended_context.get('available_characters', [])}
--------------------------------
Доступные локации: {extended_context.get('available_locations', [])}
--------------------------------

КРИТИЧНО - АНАЛИЗ ПРОСТРАНСТВЕННЫХ ОТНОШЕНИЙ:
Обращай ОСОБОЕ ВНИМАНИЕ на ключевые фразы в описании сцены и кадра:
- "перед ним/ней" → главный персонаж спиной к камере (camera_position: "behind", character_orientation: "facing_away")
- "за ним/ней" → камера находится впереди персонажа; направление корпуса/взгляда выводи по реальной цели внимания, а не автоматически в объектив
- "возвышается перед" → персонаж спиной к камере, объект впереди него
- "лицом к" → определяет направление взгляда персонажа
- "стоит напротив" → персонажи лицом друг к другу
- "поворачивается к" → изменение ориентации
- "смотрит на" → направление взгляда определяет ориентацию

УЧИТЫВАЙ КОНТЕКСТ:
- Время суток для определения освещения
- Настроение для выбора цветовой палитры и стиля
- Темпоритм для планирования движений камеры
- Соседние кадры для обеспечения преемственности

Определи технические параметры для оптимальной съемки этого кадра."""

    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=8000,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        
        clean_resp = extract_json_from_markdown(response)
        result = json.loads(clean_resp)
        return result
        
    except Exception as e:
        logger.error(f"❌ Ошибка технического анализа кадра: {e}")
        return None

def _analyze_end_shot_technical(
    start_technical_params: Dict[str, Any],
    extended_context: Dict[str, Any],
    video_prompt: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Технический анализ END кадра на основе START параметров и storyboard truth.
    video_prompt здесь опционален и может использоваться только как дополнительная подсказка,
    но не как источник истины для финального состояния.
    Определяет финальное состояние всех технических параметров.
    """
    
    system_prompt = """
=== LAYER A: ROLE + CONTRACT ===
Ты -- технический директор. Анализ КОНЕЧНОГО состояния END кадра (T=final).
Глоссарий: shot_size=крупность, ratio=масштаб (1.0=без изменений, >1.0=приближение), delta=% изменения [-60..+60].

**[P0] SHOT_FRAME_SPEC = SOURCE OF TRUTH:**
END-кадр обязан оставаться в границах shot_frame_spec: primary_subject, visible_characters, must_show, must_not_show. Чужие объекты/персонажи из video_prompt или scene-level контекста, отсутствующие в shot_frame_spec, -- ошибка.

**[P0] STORYBOARD LOCKS (camera_plan + shot_description):**
- camera_plan задает БАЗОВУЮ крупность (START=T=0). END может измениться, если shot_description/camera_plan/video_prompt явно требуют движения.
- final_shot_size согласуется: shot_description/camera_plan/shot_frame_spec, затем optional video_prompt.
- PRIMARY SUBJECT LOCK: главный субъект из shot_description. Переключение на другого запрещено.

PRIMARY SOURCE OF TRUTH: END выводится из shot_description + shot_frame_spec. Если video_prompt пустой -- восстанови END как стоп-кадр shot_description, не выдумывай динамику. Если video_prompt есть -- только как вторичная transition-подсказка в пределах shot_frame_spec.

=== LAYER B: STEP-BY-STEP ALGORITHM ===

**ШАГ 1 -- RESULT STATE:** shot_description + shot_frame_spec.must_show = стоп-кадр результата END.
- "Tongue shoots out" -> "tongue fully extended"; "Character falls" -> "character lying on ground"; "Punch connects" -> "fist extended, impact point"
- Все параметры наследуются из START, если shot_description/shot_frame_spec не указывают изменений.

**ШАГ 2 -- DELTA/RATIO CALCULATION:**
- При ЛЮБОМ движении (dolly/zoom/crane/closer/farther/approaches/pulls back) -> ОБЯЗАТЕЛЬНО числовые дельты. НЕ оставляй delta=0 при движении!
- Камера + персонаж в ОДНОМ направлении -> СЛОЖЕНИЕ; в РАЗНЫХ -> КОМПЕНСАЦИЯ. Пример: "dolly in +30% + steps back -10%" = delta +20%, ratio 1.3/0.9 ~ 1.4.
- ОГРАНИЧЕНИЯ: delta in [-60,+60] целое, ratio in [0.5,2.0] округленное до 0.1.
- ИЗМЕНЕНИЕ ПОЗЫ ПРИ СТАТИЧНОЙ КАМЕРЕ (stands up/sits down/kneels): меняет РАКУРС (pitch), НЕ крупность! delta=0, ratio=1.0, фон неизменен.
  * Камера на месте -> персонаж может быть частично обрезан, изменить final_spatial_composition.
  * Камера следит -> меняется final_camera_pitch_deg и final_camera_angle: "stands up" -> low-angle (pitch<0); "sits down" -> high-angle (pitch>0).
  * Паттерны: "rises from the sofa", "stands up", "gets up", "встает", "поднимается" -> ракурс, НЕ масштаб!

**ШАГ 3 -- FINAL SHOT SIZE (эскалация по ratio):**
- ratio >= 1.6 -> +2 ступени; >= 1.3 -> +1; <= 0.6 -> -2; <= 0.8 -> -1.
- Ступени: Extreme wide > Wide > Medium wide > Medium > Medium close-up > Close-up > Extreme close-up.

**ШАГ 4 -- FINAL CAMERA/LIGHTING:**
- Pan/Tilt с углами -> final_camera_yaw_deg/pitch_deg. "handheld/shaky" -> stability=transitional/unstable. "locks off/settles" -> completed=true, stable.
- Over-shoulder -> camera_position=behind, orientation=facing_away. "смотрит на объект" -> side/behind, facing_away/profile.
- facing_camera допустим ТОЛЬКО при явном viewer-facing/direct-address. Иначе -- взгляд на источник события / вдоль траектории.
- Фоновая динамика: ДАЖЕ при delta=0, ratio=1.0 анализируй video_prompt на фоновые процессы (падение, горение, рассеивание и т.д.). spatial_changes_from_start НЕ МОЖЕТ быть пустым при любой динамике в video_prompt.

**ШАГ 5 -- CONTINUITY SCORE:**
score = 5 + 2*(location_unchanged) + 2*(characters_unchanged) + 1*(lighting_unchanged) - delta_penalty - 1*(per new_character).
delta_penalty: fast/intense pacing AND delta>40 -> 1; climax/reveal -> 0; else -> 2. Clamp [1,10].

**ШАГ 6 -- LINKING DECISION:**
- composition_stability: stable (delta=0, ratio~1.0, нет активных действий); transitional (delta<=15%, остаточные движения/поза); unstable (delta>15% или ratio вне [0.9,1.1]).
- camera_movement_completed: true/false.
- next_shot_compatibility: описание совместимости.

**[P1] FACE/GAZE DERIVATION (END):**
- Если лицо читается в END -> final_spatial_composition фиксирует итоговое выражение лица и взгляд, даже если end_state_spec пуст.
- pose_signature/character_pose_signatures из end_state_spec -- authoritative; не flatten к neutral, не переноси между персонажами.
- Выводи из shot_description, shot_frame_spec, world_physics и video_prompt как вторичной подсказки: итоговая микро-реакция соответствует финальному состоянию мира.
- Не default к "neutral"/"blank"/"pause face" при потере опоры, ударе, срыве равновесия, угрозе.

**[P1] PHYSICAL LEGIBILITY LOCK (END):**
- Если нужны одновременно читаемое лицо И видимая причинная связь (опора/проем/контакт) -> final_shot_size/delta/ratio достаточно умеренные. Предпочитай на шаг шире, чем тесный crop с разрушенной причинной геометрией.

**[P1] SCENE_CONTINUITY_FACTS:** применяй если уместны по camera_plan и не противоречат shot_description. Запрещено: токсичность/мат/искажение имен.

**PROP CONTINUITY:** Перечисли объекты START -> сопоставь с video_prompt (снял/положил/поднял) -> {kept, removed, added, notes с локациями}.
- Объекты не исчезают без действия. Локации: on table/in pocket/in hand/on floor/hung on hook/in bag/on belt/in holster/on workbench/leaning against wall. Нестандартная -> кратко опиши.
- Гардероб -- ТОЛЬКО если явно в video_prompt. Идентичность материала сохраняется (organic->organic, mechanical->mechanical).

=== LAYER C: REFERENCE TABLES ===

**ТАБЛИЦА ТРАНСЛЯЦИИ ДВИЖЕНИЯ:**
| Тип движения | Модификатор | delta_% | ratio |
|---|---|---|---|
| Dolly/Zoom IN | slightly/moderately/dramatically | 15-25 / 25-40 / 40-60 | 1.1-1.3 / 1.3-1.6 / 1.6-2.0 |
| Dolly/Zoom OUT | slightly/moderately | -15..-25 / -25..-40 | 0.7-0.9 / 0.5-0.7 |
| Персонаж к камере | steps closer / approaches | +10..+20 / +25..+40 | 1.1-1.25 / 1.3-1.55 |
| Персонаж от камеры | steps back | -10..-15 | 0.85-0.95 |
| Pan/Tilt/Rotate/Crane/Tracking/Focus pull | любой | 0 | 1.0 |
Калибровка: slightly=10-20%, moderately=25-40%, dramatically=40-60%.

**КАЛИБРОВОЧНЫЕ ПРИМЕРЫ:**
- "dolly in + subject steps back" -> delta~+10..+15% (компенсация), ratio~1.10-1.15.
- "Pan right 30, tilt up 10, handheld" -> delta=0, ratio=1.0; yaw=+30, pitch=+10; transitional.
- "Dolly in moderate + subject approaches" -> delta~+35..+45% (усиление), ratio~1.40-1.70.
- "Static camera; subject stands up" -> delta=0, ratio=1.0, pitch меняется (low-angle). Фон неизменен.
- "Static camera; subject sits down" -> delta=0, ratio=1.0, pitch меняется (high-angle). Фон неизменен.
- "Camera locks off, subject settles" -> completed=true, stable, delta=0, ratio=1.0.

**JSON OUTPUT FORMAT:**
{"characters":[],"location":"","final_shot_size":"","final_camera_angle":"","final_lighting_style":"","final_color_palette":"","final_camera_position":"behind/in_front/side/above/below","final_character_orientation":"facing_camera/facing_away/profile/three_quarter","final_spatial_composition":"","final_point_of_view":"objective/subjective/pov_name","spatial_changes_from_start":"детальное описание ВСЕХ изменений включая фоновую динамику","camera_movement_completed":"true/false","composition_stability":"stable/transitional/unstable","continuity_score":"1-10","next_shot_compatibility":"","framing_delta_percent":"int [-60,+60]","subject_scale_ratio":"float [0.5,2.0]","final_camera_yaw_deg":"int [-180,180]","final_camera_pitch_deg":"int [-90,90]","final_subject_yaw_deg":"int [-180,180]","final_focus_target":"foreground/midground/background/name","final_depth_of_field":"shallow/normal/deep","main_subject":"","final_depth_order":["foreground:x","midground:x","background:x"],"prop_continuity":{"kept":[],"removed":[],"added":[],"notes":""}}"""

    # Подготавливаем данные для анализа
    start_summary = {
        "characters": start_technical_params.get("characters", []),
        "location": start_technical_params.get("location", ""),
        "camera_position": start_technical_params.get("camera_position", ""),
        "character_orientation": start_technical_params.get("character_orientation", ""),
        "spatial_composition": start_technical_params.get("spatial_composition", ""),
        "shot_size": start_technical_params.get("shot_size", ""),
        "camera_angle": start_technical_params.get("camera_angle", ""),
        "lighting_style": start_technical_params.get("lighting_style", ""),
        "color_palette": start_technical_params.get("color_palette", "")
    }
    
    context_summary = {
        "scene_mood": extended_context.get("scene_mood", ""),
        "lighting_context": extended_context.get("lighting_context", ""),
        "scene_pacing": extended_context.get("scene_pacing", ""),
        "narrative_position": extended_context.get("narrative_position", ""),
        "next_shot": extended_context.get("next_shot", {}),
        "previous_shot": extended_context.get("previous_shot", {}),
        "current_shot_position": extended_context.get("current_shot_position", "")
    }
    
    user_prompt = f"""Проанализируй конечное состояние кадра:

STORYBOARD (истина для текущего кадра):
- camera_plan: {extended_context.get('camera_plan','')}
- shot_description: {extended_context.get('shot_description','')}
- scene_continuity_facts (устойчивые факты сцены; применять если уместно по camera_plan и не противоречит shot_description):
{json.dumps(extended_context.get('scene_continuity_facts', {}), ensure_ascii=False)}
- shot_frame_spec (авторитетный source of truth для этого шота):
{json.dumps(extended_context.get('shot_frame_spec', {}), ensure_ascii=False, indent=2)}

НАЧАЛЬНЫЕ ПАРАМЕТРЫ (START):
<start>
{json.dumps(start_summary, ensure_ascii=False, indent=2)}
</start>

TRANSITION HINT (optional video_prompt; may be empty):
<video_prompt>
{video_prompt}
</video_prompt>

КОНТЕКСТ ВСЕЙ СЦЕНЫ:
<scene_context>
{json.dumps(context_summary, ensure_ascii=False, indent=2)}
</scene_context>

ЗАДАЧА: Определи, как выглядят ВСЕ технические параметры END-кадра.
Если video_prompt пустой, выводи END напрямую из shot_description + shot_frame_spec.
Особое внимание удели:
1. Завершенности движений камеры
2. Стабильности итоговой композиции  
3. Совместимости с потенциальным следующим кадром
4. Оценке преемственности с START состоянием
5. КОЛИЧЕСТВЕННОЙ смене крупности (framing_delta_percent, subject_scale_ratio) на основе shot_description/camera_plan и optional video_prompt
6. Континуити реквизита (prop_continuity) на основе ПРАВИЛ РЕКВИЗИТА

**КРИТИЧНО - СУММИРОВАНИЕ ЭФФЕКТОВ**:
- Если `video_prompt` сочетает движение камеры и персонажа вдоль оптической оси, рассчитай СУММАРНЫЕ `framing_delta_percent` и `subject_scale_ratio` (учитывай компенсации и усиления).
- Для pan/tilt/rotate/swivel с явными углами — выведи `final_camera_yaw_deg`/`final_camera_pitch_deg`; для поворота персонажа с числом градусов — `final_subject_yaw_deg`.
- Если присутствуют "handheld/shaky/rolling", отрази это в composition_stability; если кадр "locks off/settles" в конце — camera_movement_completed=true.
- Если указаны "rack focus/focus pulls" — заполни `final_focus_target` и `final_depth_of_field`.
- Если персонажей несколько — укажи `main_subject` и `final_depth_order`.

**КРИТИЧНО**: При ЛЮБОМ явном упоминании движения в shot_description/camera_plan/optional video_prompt ("dolly", "zoom", "crane", "pan", "tilt", "slowly", "closer", "farther", "in", "out", "up", "down", "moves", "approaches", "pulls back", "tighter", "wider") — ОБЯЗАТЕЛЬНО установи числовые дельты framing_delta_percent и subject_scale_ratio. НЕ оставляй 0 при наличии движения!
**КРИТИЧНО**: При ЛЮБОМ упоминании поворота камеры в video_prompt определи угол поворота камеры и финальное положение персонажей относительно нее.
Проанализируй каждый параметр и определи его ФИНАЛЬНОЕ значение."""

    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=8000,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        
        clean_resp = extract_json_from_markdown(response)
        result = json.loads(clean_resp)
        return result
        
    except Exception as e:
        logger.error(f"❌ Ошибка технического анализа END кадра: {e}")
        return None

def _optimize_reference_images(shot_data: dict) -> dict:
    """
    Оптимизирует референсные изображения на основе анализа финального english_prompt.
    Исключает неиспользуемые референсы для повышения производительности и точности.
    """
    logger.info(f"🚀 OPTIMIZER START: Processing {shot_data.get('shot_type', 'unknown')} shot")
    
    english_prompt = shot_data.get('english_prompt', '')
    reference_paths = shot_data.get('reference_image_paths', [])
    roles_instruction = shot_data.get('reference_roles_instruction', '')
    
    logger.info(f"🔍 OPTIMIZER DEBUG: roles_instruction = '{roles_instruction}'")
    logger.info(f"🔍 OPTIMIZER DEBUG: first_ref = '{reference_paths[0] if reference_paths else 'None'}'")
    logger.info(f"🔍 OPTIMIZER DEBUG: is_continuity = {'continuity reference' in roles_instruction.lower()}")
    
    
    if not english_prompt or not reference_paths:
        logger.info(f"🔍 OPTIMIZER DEBUG: prompt='{english_prompt[:50]}...', refs={len(reference_paths)}")
        logger.info(f"⏭️ OPTIMIZER SKIP: No prompt or references")
        return shot_data
    
    system_prompt = (
        "Ты — оптимизатор референсных изображений для AI-редактирования. Проанализируй english_prompt и определи, какие референсные изображения реально используются.\n\n"
        "ПРАВИЛА АНАЛИЗА (в строгом порядке приоритета):\n"
        "1. **ИДЕНТИЧНОСТЬ ПЕРСОНАЖА** (БЕЗУСЛОВНЫЙ ПРИОРИТЕТ): Если в кадре присутствует named персонаж (из списка `ПЕРСОНАЖИ В КАДРЕ` / `characters`) — ТЫ ОБЯЗАН СОХРАНИТЬ его Character Reference.\n"
        "   ЗАПРЕЩЕНО использовать только image 1, если есть персонаж: continuity reference (image 1) часто искажает идентичность.\n"
        "   НЕЛЬЗЯ выкидывать character reference даже для CLOSE UP / EXTREME CLOSE UP / MACRO: если персонаж присутствует в кадре, его reference обязателен.\n"
        "2. **ПРОСТОЕ МАСШТАБИРОВАНИЕ** (высокий приоритет): Только если это пейзаж или объект БЕЗ персонажа. Команды 'Increase image 1', 'Move subject away'.\n"
        "3. **КРУПНЫЙ ПЛАН С CONTINUITY**: Только для деталей (руки, ноги) или если персонажа НЕТ в кадре.\n"
        "4. **СОЗДАНИЕ С НУЛЯ** (высокий приоритет): команды 'Create', 'Generate' БЕЗ условий п.1 → ОСТАВИТЬ все изображения\n"
        "5. **КОМПОЗИТИНГ** (средний приоритет): команды 'combine', 'merge' → НУЖНЫ соответствующие изображения\n"
        "5. **КОМАНДЫ ИДЕНТИЧНОСТИ** (низкий приоритет): упоминания 'preserve facial identity', 'maintain character', 'preserve identity' → НУЖЕН character reference\n"
        "6. **КОМАНДЫ СТИЛЯ/АТМОСФЕРЫ** (низкий приоритет): упоминания 'match lighting', 'consistent style', 'natural shadows', 'color temperature' → НУЖЕН location reference\n"
        "7. **АЛЬТЕРНАТИВНЫЙ РАКУРС ПЕРСОНАЖА** (высокий приоритет): если character_orientation = 'facing_away'/'profile'/'three_quarter' ИЛИ final_character_orientation = 'facing_away'/'profile'/'three_quarter' ИЛИ упоминания 'from behind', 'back view', 'rear view', 'side view', 'profile view', 'three quarter' → ОБЯЗАТЕЛЬНО НУЖЕН character reference! Continuity reference может не содержать требуемый ракурс персонажа.\n"
        "8. **СМЕНА РАКУРСА/ПОВОРОТ КАМЕРЫ** (ВЫСОКИЙ приоритет): Если video_prompt описывает PAN, TILT, ROTATION или смену угла (Low angle → High angle) — НУЖЕН Location Reference! Image 1 (continuity) содержит только старый ракурс. Чтобы дорисовать окружение при повороте, нужна полная локация.\n"
        "9. **ПУСТАЯ СТАТИЧНАЯ СЦЕНА** (ТОЛЬКО ФОНЫ): Применяй ТОЛЬКО если в кадре НЕТ НИ ОДНОГО ПЕРСОНАЖА. framing_delta_percent = 0. Если есть персонаж — см. п.1. ЗАПРЕЩЕНО применять к сценам с персонажами!\n"
        "10. **ЧИСТОЕ РЕДАКТИРОВАНИЕ** (базовый): только 'crop', 'reposition', 'adjust' без упоминания идентичности/стиля → ТОЛЬКО image 1\n"
        "10. **УГЛЫ/ФОКУС**: если упомянуты yaw/pitch или rack focus/depth of field — это НЕ требует дополнительных референсов; удерживай только image 1, если нет явных команд композита/идентичности/стиля.\n\n"
        "**КРИТИЧЕСКИ ВАЖНО**: Различай ПРОСТОЕ и КОМПЛЕКСНОЕ масштабирование!\n"
        "**ПРИМЕРЫ ПРОСТОГО МАСШТАБИРОВАНИЯ** (только image 1):\n"
        "- 'Increase image 1 by 1.2x; ensure consistent lighting' → ТОЛЬКО image 1 (базовая континуити)\n"
        "- 'Move subject away by 0.8x; maintain quality' → ТОЛЬКО image 1 (простое уменьшение)\n\n"
        "**ПРИМЕРЫ КОМПЛЕКСНОГО РЕДАКТИРОВАНИЯ** (анализируй дальше):\n"
        "- 'Increase image 1 by 1.25x; reposition camera from side to behind; adjust lighting to warm backlight' → КОМПЛЕКСНОЕ (изменение камеры + освещения)\n"
        "- 'Move subject away by 0.8x; shift character orientation to three_quarter; enhance atmospheric effects' → КОМПЛЕКСНОЕ (ориентация + атмосфера)\n"
        "- 'Increase image 1 by 1.2x; position subject center; reduce background dominance; preserve gloves' → КОМПЛЕКСНОЕ (позиционирование + фон)\n\n"
        "**ПРИМЕРЫ КРУПНЫХ ПЛАНОВ С CONTINUITY**:\n"
        "- 'Create close-up of Father's hands' + КРУПНЫЙ ПЛАН + первый референс из предыдущего кадра → ТОЛЬКО image 1 (continuity reference содержит все нужные детали)\n"
        "- 'Generate detailed close-up of character hands' + CLOSE SHOT + image 1 = continuity reference → ТОЛЬКО image 1 (character/location референсы избыточны)\n"
        "- 'Create medium shot of character' + СРЕДНИЙ ПЛАН → ОСТАВИТЬ все (для средних планов нужна композиция)\n\n"
        "**ПРИМЕРЫ АЛЬТЕРНАТИВНОГО РАКУРСА ПЕРСОНАЖА**:\n"
        "- 'Edit image 1: Increase image 1 by 1.2x; position character from behind' + final_character_orientation = 'facing_away' → НУЖНЫ image 1 + character reference (для вида со спины)\n"
        "- 'Reposition camera to side view' + character_orientation = 'profile' → НУЖЕН character reference (continuity может не содержать профиль)\n"
        "- 'Create three quarter view of character' + final_character_orientation = 'three_quarter' → ОБЯЗАТЕЛЬНО НУЖЕН character reference\n"
        "- 'Show character in profile view' → НУЖЕН character reference (любой ракурс кроме facing_camera)\n\n"
        "**ЛОГИКА**: Масштабирование работает с уже готовой композицией (image 1), которая УЖЕ содержит все элементы сцены. Дополнительные референсы не нужны и только замедляют обработку.\n"
        "Фокус и углы (yaw/pitch) не требуют дополнительных референсов: это параметры камеры.\n\n"
        "**АЛГОРИТМ ДЕТЕКЦИИ (строго по порядку)**:\n"
        "1. МАСШТАБИРОВАНИЕ: ищи команды 'Increase image 1 by', 'Move subject away by' → если найдены, проверь дополнительные команды:\n"
        "   - Только масштабирование + базовые качества ('ensure consistent', 'maintain quality') → ПРОСТОЕ (только image 1)\n"
        "   - Есть команды камеры/освещения/позиции ('reposition', 'camera', 'lighting', 'from behind', 'three quarter') → КОМПЛЕКСНОЕ (анализируй далее по п.3-6)\n"
        "2. КРУПНЫЙ ПЛАН: если НЕТ простого масштабирования, проверь CAMERA_PLAN содержит 'КРУПНЫЙ' или 'CLOSE' И первый референс — continuity → ТОЛЬКО image 1, НО ТОЛЬКО ЕСЛИ в кадре НЕТ персонажа.\n"
        "3. АЛЬТЕРНАТИВНЫЙ РАКУРС: если НЕТ п.1-2, проверь character_orientation = 'facing_away'/'profile'/'three_quarter' ИЛИ final_character_orientation = 'facing_away'/'profile'/'three_quarter' ИЛИ упоминания альтернативных ракурсов → НУЖЕН character reference\n"
        "4. СТАТИЧНАЯ КАМЕРА: если НЕТ п.1-3, проверь framing_delta_percent = 0 И subject_scale_ratio = 1.0 И camera_movement_completed = true И НЕТ команд масштабирования → ТОЛЬКО image 1\n"
        "5. СОЗДАНИЕ С НУЛЯ: если НЕТ п.1-4, но есть 'Create'/'Generate' → ОСТАВИТЬ все изображения\n"
        "6. Если НЕТ п.1-5 — анализируй остальные правила (композитинг, идентичность, стиль)\n\n"
        "СТРАТЕГИЯ: При неясности между правилами одного уровня → ОСТАВЛЯЙ референс (консервативный подход).\n\n"
        "РЕЗУЛЬТАТ: верни JSON с полями:\n"
        "- keep_indices: список индексов (0-based) изображений для сохранения\n"
        "- reasoning: краткое объяснение решения\n"
        "- optimization_type: тип оптимизации ('scaling_only', 'complex_editing', 'creation', 'identity_required', 'style_required', 'full_editing', 'conservative', 'close_up_continuity', 'character_alt_angle', 'static_camera')"
    )
    
    current_references = shot_data.get('reference_image_paths', [])
    roles_instruction = shot_data.get('reference_roles_instruction', '')
    
    camera_plan = shot_data.get('camera_plan', '').upper()
    shot_type = shot_data.get('shot_type', 'unknown')
    video_prompt = shot_data.get('video_prompt', '')

    characters_context = ""
    if shot_data.get("characters"):
        characters_context = "ПЕРСОНАЖИ В КАДРЕ:" + chr(10) + chr(10).join([f"- {c.get('name')} ({c.get('role')})" for c in shot_data["characters"]])
    
    user_prompt = f"""
Проанализируй этот english_prompt и определи оптимальный набор референсных изображений:

{characters_context}

ENGLISH_PROMPT: "{shot_data['english_prompt']}"

VIDEO_PROMPT (ДИНАМИКА/КАМЕРА): "{video_prompt}"

CAMERA_PLAN: "{camera_plan}" (тип кадра: {shot_type})

ХАРАКТЕРИСТИКИ КАДРА:
- character_orientation: "{shot_data.get('character_orientation', 'unknown')}" (для START кадров)
- final_character_orientation: "{shot_data.get('final_character_orientation', 'unknown')}" (для END кадров)
- final_camera_position: "{shot_data.get('final_camera_position', 'unknown')}"
- framing_delta_percent: "{shot_data.get('framing_delta_percent', 0)}"
- subject_scale_ratio: "{shot_data.get('subject_scale_ratio', 1.0)}"
- camera_movement_completed: "{shot_data.get('camera_movement_completed', 'unknown')}"

ТЕКУЩИЕ РЕФЕРЕНСЫ ({len(current_references)} изображений):
{chr(10).join([f"{i+1}. {ref}" for i, ref in enumerate(current_references)])}

ТЕКУЩАЯ ИНСТРУКЦИЯ РОЛЕЙ: "{roles_instruction}"

АНАЛИЗ КРУПНОСТИ ПЛАНА И ИДЕНТИЧНОСТИ:
- ВАЖНО: Если в кадре есть персонаж, ВСЕГДА сохраняй character reference, даже для крупных планов (CLOSE UP). Continuity reference (image 1) часто недостаточно для сохранения идентичности при смене ракурса/освещения.
- ИСКЛЮЧЕНИЕ: Только "MACRO" (детали) или "EXTREME CLOSE UP" могут обойтись без character reference.
- Более общие планы (ОБЩИЙ, СРЕДНИЙ, WIDE, MEDIUM) ОБЯЗАТЕЛЬНО требуют дополнительных референсов для композиции

АНАЛИЗ ОРИЕНТАЦИИ ПЕРСОНАЖА:
- Если character_orientation = "facing_away"/"profile"/"three_quarter" (START) ИЛИ final_character_orientation = "facing_away"/"profile"/"three_quarter" (END) ИЛИ упоминания альтернативных ракурсов ("from behind", "back view", "side view", "profile view", "three quarter") в english_prompt → ОБЯЗАТЕЛЬНО НУЖЕН character reference!
- Continuity reference из предыдущего кадра может содержать персонажа только в одном ракурсе
- Character reference содержит все виды персонажа (лицом, в профиль, спиной, три четверти)
- ИСКЛЮЧЕНИЕ: Только при character_orientation = "facing_camera" можно обойтись continuity reference

ДЕТЕКЦИЯ CONTINUITY REFERENCE:
- В инструкции ролей есть "continuity reference" ИЛИ
- Первый путь содержит "/97_shots/" и заканчивается на ".png" (сгенерированный кадр из предыдущего shot)

Определи, какие изображения реально нужны для выполнения команд в english_prompt.
"""

    try:
        
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_ultimate,
            max_tokens=8000,
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        
        logger.info(f"🔍 OPTIMIZER DEBUG: Raw response = {response}")
        
        # Если response - строка, нужно парсить JSON
        if isinstance(response, str):
            try:
                import json
                clean_resp = extract_json_from_markdown(response)
                response = json.loads(clean_resp)
                logger.info(f"🔍 OPTIMIZER DEBUG: Parsed JSON = {response}")
            except Exception as e:
                logger.error(f"❌ OPTIMIZER ERROR: Failed to parse JSON: {e}")
                return shot_data
        
        if response and response.get('keep_indices') is not None:
            keep_indices = response['keep_indices']
            reasoning = response.get('reasoning', 'No reasoning provided')
            optimization_type = response.get('optimization_type', 'unknown')
            
            # Применяем оптимизацию
            logger.info(f"🔍 OPTIMIZER CHECK: keep_indices={keep_indices}, current_refs={len(current_references)}")
            if len(keep_indices) < len(current_references):
                optimized_references = [current_references[i] for i in keep_indices if i < len(current_references)]
                logger.info(f"🔍 OPTIMIZER APPLIED: {len(current_references)} → {len(optimized_references)} refs")

                # ============================================================
                # ЖЁСТКАЯ ГАРАНТИЯ: если в кадре есть персонажи — их character refs НЕ МОГУТ БЫТЬ УДАЛЕНЫ
                # ============================================================
                try:
                    MAX_TOTAL_IMAGES = 10
                    # character refs в исходном порядке (как были собраны до оптимизации)
                    required_char_refs_in_order = []
                    try:
                        char_ref_paths_local = {
                            (c.get("reference_image_path") or "").strip()
                            for c in (shot_data.get("characters") or [])
                            if (c.get("reference_image_path") or "").strip()
                            and "/97_shots/" not in (c.get("reference_image_path") or "")
                        }
                        required_char_refs_in_order = [r for r in current_references if r in char_ref_paths_local]
                    except Exception:
                        required_char_refs_in_order = []
                    missing_char_refs = [r for r in required_char_refs_in_order if r not in optimized_references]
                    if missing_char_refs:
                        shot_id = f"scene_{shot_data.get('scene_number','X')}_shot_{shot_data.get('shot_number','X')}"
                        logger.warning(
                            f"🛡️ CHARACTER REF LOCK: оптимизатор попытался убрать character refs {missing_char_refs} "
                            f"для {shot_type.upper()} {shot_id} — возвращаем обратно"
                        )
                        insert_pos = 1 if (optimized_references and "/97_shots/" in str(optimized_references[0])) else 0
                        for r in missing_char_refs:
                            optimized_references.insert(insert_pos, r)
                            insert_pos += 1

                        # если превысили лимит — выкидываем только НЕ-защищённые референсы (в первую очередь локацию/прочие)
                        protected = set(required_char_refs_in_order)
                        if optimized_references and "/97_shots/" in str(optimized_references[0]):
                            protected.add(str(optimized_references[0]))
                        while len(optimized_references) > MAX_TOTAL_IMAGES:
                            removed_any = False
                            for idx in range(len(optimized_references) - 1, -1, -1):
                                if str(optimized_references[idx]) not in protected:
                                    optimized_references.pop(idx)
                                    removed_any = True
                                    break
                            if not removed_any:
                                break
                except Exception as _e:
                    logger.warning(f"⚠️ CHARACTER REF LOCK failed: {_e}")
                
                # Обновляем инструкцию ролей
                if len(optimized_references) == 1:
                    # Один референс - обычно continuity reference
                    optimized_roles = "Use Image 1 as the strict continuity base. Preserve all details unless explicitly modified by the prompt."
                elif optimization_type in ['scaling_only', 'close_up_continuity', 'static_camera']:
                    optimized_roles = "Use Image 1 as the strict continuity base. Preserve all details unless explicitly modified by the prompt."
                elif optimization_type == 'complex_editing':
                    # Для комплексного редактирования сохраняем исходную инструкцию ролей
                    optimized_roles = roles_instruction
                elif optimization_type == 'character_alt_angle':
                    # Для альтернативного ракурса персонажа сохраняем исходную инструкцию ролей
                    optimized_roles = roles_instruction
                else:
                    # Сохраняем исходную инструкцию ролей (LLM уже сгенерировал правильную)
                    optimized_roles = roles_instruction
                
                # Обновляем english_prompt с оптимизированными ролями
                english_prompt = shot_data.get('english_prompt', '')
                
                # Удаляем старую инструкцию ролей
                if '\n\nUse ' in english_prompt:
                    main_prompt = english_prompt.split('\n\nUse ')[0]
                    optimized_english_prompt = f"{main_prompt}\n\n{optimized_roles}"
                else:
                    optimized_english_prompt = english_prompt
                
                # Логируем оптимизацию
                shot_type = shot_data.get('shot_type', 'unknown')
                shot_id = f"scene_{shot_data.get('scene_number', 'X')}_shot_{shot_data.get('shot_number', 'X')}"
                logger.info(f"🎯 REFERENCE OPTIMIZATION [{shot_type.upper()} {shot_id}]: {len(current_references)} → {len(optimized_references)} images")
                logger.info(f"   Type: {optimization_type}")
                logger.info(f"   Reasoning: {reasoning}")
                logger.info(f"   Kept: {[i+1 for i in keep_indices]}")
                
                return {
                    **shot_data,
                    'english_prompt': optimized_english_prompt,
                    'reference_image_paths': optimized_references,
                    'reference_roles_instruction': optimized_roles,
                    '_optimization_applied': True,
                    '_optimization_type': optimization_type,
                    '_optimization_reasoning': reasoning,
                    '_original_reference_count': len(current_references),
                    '_optimized_reference_count': len(optimized_references)
                }
            else:
                print(f"🔄 REFERENCE OPTIMIZATION [{shot_data.get('shot_type', 'unknown').upper()}]: No optimization needed ({optimization_type})")
        
        return shot_data
        
    except Exception as e:
        print(f"⚠️ Reference optimization failed: {e}")
        return shot_data

def _smart_location_match_llm(
    location_from_llm: str, 
    available_locations: List[Dict[str, Any]], 
    scene_number: int, 
    shot_number: int
) -> Optional[Dict[str, Any]]:
    """Умное сопоставление локации через LLM"""
    
    if not available_locations:
        return None
    
    # Подготавливаем описания доступных локаций
    locations_summary = []
    for i, loc in enumerate(available_locations):
        locations_summary.append(f"{i+1}. {loc.get('name', 'Unnamed')}: {loc.get('description', '')[:100]}...")
    
    system_prompt = f"""Ты - аналитик сценариев. Определи, какая из доступных локаций лучше всего соответствует требуемой.

ТРЕБУЕМАЯ ЛОКАЦИЯ: "{location_from_llm}"
СЦЕНА/ШОТ: {scene_number}/{shot_number}

ДОСТУПНЫЕ ЛОКАЦИИ:
{chr(10).join(locations_summary)}

ПРАВИЛА СОПОСТАВЛЕНИЯ:
- ТОЧНЫЕ совпадения имеют приоритет
- ЧАСТИЧНЫЕ совпадения допустимы (например: "детская комната" ↔ "детская комната будущего")
- Учитывай КЛЮЧЕВЫЕ СЛОВА, не только полное название
- Игнорируй незначительные различия ("Андрея Петровича", "подъезда", "будущего")

ПРИМЕРЫ:
- "детская комната" → подходит к "детская комната будущего"
- "кухня" → подходит к "кухня Андрея Петровича"
- "коридор" → подходит к "коридор подъезда"

ЗАДАЧА:
Если есть подходящая локация - верни её номер (1-{len(available_locations)}).
Если НИ ОДНА локация НЕ подходит - верни 0.

ВЕРНИ ТОЛЬКО ЧИСЛО (1-{len(available_locations)} или 0)."""

    try:
        resp = call_openai_api(
            prompt=system_prompt,
            system_prompt=f"Ты эксперт по анализу локаций. Анализируешь сцену {scene_number}, шот {shot_number}.",
            model=model_ultimate,
            max_tokens=10,
            temperature=0.1
        )
        
        # Извлекаем номер
        match = re.search(r'\b(\d+)\b', resp.strip())
        if match:
            location_index = int(match.group(1))
            if 1 <= location_index <= len(available_locations):
                return available_locations[location_index - 1]
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Ошибка LLM сопоставления локации: {e}")
        return None

def _create_shot_item(
    project_id: str,
    scene_number: int,
    shot_number: int,
    shot_type: str,
    page_number: int,
    item_number: int,
    camera_plan: str,
    timing: str,
    llm_result: Dict[str, Any],
    characters_data: List[Dict[str, Any]],
    locations_data: List[Dict[str, Any]],
    location_time: str = "",
    location_canon_name: str = "",
    scene_action: str = "",
    shot_description: str = "",
    shot_frame_spec: Optional[Dict[str, Any]] = None,
    shot_frame_spec_cache_key: str = "",
    scene_continuity_facts: Optional[Dict[str, Any]] = None,
    language: str = "en",
    is_linked_start: bool = False,
    seed: Optional[int] = None,
    visual_style: str = "",
    style_do_not_include: Any = None,
) -> Dict[str, Any]:
    """
    Создает элемент для shots.json на основе результата LLM.
    """

    language = str(language or "en").strip().lower() or "en"
    _is_bs = black_screen_storyboard_shot(camera_plan, shot_frame_spec)
    prompt_edit_prefixes = {
        "ru": "Редактируй image 1:",
        "en": "Edit image 1:",
        "es": "Edita la imagen 1:",
        "fr": "Modifie l'image 1:",
        "de": "Bearbeite Bild 1:",
    }
    prompt_create_prefixes = {
        "ru": "Создай",
        "en": "Create",
        "es": "Crea",
        "fr": "Cree",
        "de": "Erstelle",
    }
    prompt_generate_variants = {
        "ru": ("создай", "сгенерируй"),
        "en": ("create", "generate"),
        "es": ("crea", "genera"),
        "fr": ("cree", "genere"),
        "de": ("erstelle", "generiere"),
    }
    negative_tokens = {
        "ru": {
            "text": "текст",
            "watermark": "водяной знак",
            "logo": "логотип",
            "unwanted_text": "нежелательный текст",
            "random_text": "случайный текст",
            "no_long_text": "без длинных текстов",
            "multipanel": [
                "сплит-скрин",
                "разделенный экран",
                "разделённый экран",
                "панели",
                "сетка",
                "коллаж",
                "триптих",
                "диптих",
                "несколько кадров в одном",
                "разбивка на кадры",
            ],
            "continuity_lock": [
                "без замены фоновой архитектуры",
                "без смены геометрии локации",
                "без другого задника",
                "без абстрактного фона",
                "без переноса фоновых якорей",
            ],
            "default_negative": "nsfw, водяной знак, текст, логотип, искаженные руки, лишние конечности, деформированное лицо, низкое разрешение, искаженные части",
        },
        "es": {
            "text": "texto",
            "watermark": "marca de agua",
            "logo": "logotipo",
            "unwanted_text": "texto no deseado",
            "random_text": "texto aleatorio",
            "no_long_text": "sin texto largo",
            "multipanel": [
                "pantalla dividida",
                "multipanel",
                "paneles multiples",
                "cuadricula",
                "collage",
                "tripitico",
                "diptico",
            ],
            "continuity_lock": [
                "sin reemplazar la arquitectura del fondo",
                "sin cambiar la geometria de la ubicacion",
                "sin fondo alternativo",
                "sin fondo abstracto",
                "sin mover anclas del fondo",
            ],
            "default_negative": "nsfw, marca de agua, texto, logotipo, manos distorsionadas, dedos extra, extremidades extra, baja resolucion, rostro deformado",
        },
        "fr": {
            "text": "texte",
            "watermark": "filigrane",
            "logo": "logo",
            "unwanted_text": "texte indesirable",
            "random_text": "texte aleatoire",
            "no_long_text": "pas de texte long",
            "multipanel": [
                "ecran divise",
                "multi-panneaux",
                "plusieurs panneaux",
                "grille",
                "collage",
                "triptyque",
                "diptyque",
            ],
            "continuity_lock": [
                "sans remplacer l'architecture de fond",
                "sans changer la geometrie du lieu",
                "sans decor alternatif",
                "sans fond abstrait",
                "sans deplacer les ancrages de fond",
            ],
            "default_negative": "nsfw, filigrane, texte, logo, mains deformees, doigts supplementaires, membres supplementaires, basse resolution, visage deforme",
        },
        "de": {
            "text": "text",
            "watermark": "wasserzeichen",
            "logo": "logo",
            "unwanted_text": "unerwunschter text",
            "random_text": "zufalliger text",
            "no_long_text": "kein langer text",
            "multipanel": [
                "splitscreen",
                "mehrere panels",
                "multi-panel",
                "gitter",
                "collage",
                "triptychon",
                "diptychon",
            ],
            "continuity_lock": [
                "keine ersetzung der hintergrundarchitektur",
                "keine anderung der ortsgeometrie",
                "kein anderer hintergrund",
                "kein abstrakter hintergrund",
                "keine verschiebung der hintergrundanker",
            ],
            "default_negative": "nsfw, wasserzeichen, text, logo, verzerrte hande, zusatzliche finger, zusatzliche gliedmassen, niedrige auflosung, deformiertes gesicht",
        },
        "en": {
            "text": "text",
            "watermark": "watermark",
            "logo": "logo",
            "unwanted_text": "unwanted text",
            "random_text": "random text",
            "no_long_text": "no long text",
            "multipanel": [
                "split-screen",
                "split screen",
                "multi-panel",
                "multiple panels",
                "grid",
                "collage",
                "triptych",
                "diptych",
            ],
            "continuity_lock": [
                "do not replace background architecture",
                "do not change location geometry",
                "no alternate backdrop",
                "no abstract background",
                "do not move background anchors",
            ],
            "default_negative": "nsfw, watermark, text, logo, distorted hands, extra limbs, deformed face, lowres, distorted parts",
        },
    }

    prompt_language_tokens = negative_tokens.get(language, negative_tokens["en"])
    target_edit_prefix = prompt_edit_prefixes.get(language, prompt_edit_prefixes["en"])
    target_create_prefix = prompt_create_prefixes.get(language, prompt_create_prefixes["en"])
    all_edit_prefixes = tuple(prefix.lower() for prefix in prompt_edit_prefixes.values())
    all_create_prefixes = tuple(
        variant
        for variants in prompt_generate_variants.values()
        for variant in variants
    )
    
    # Формируем путь сохранения с новым именем файла
    output_path = f"plots/storybooks/{project_id}/97_shots/scene_{scene_number:02d}_shot_{shot_number:02d}/img_final_{shot_type}_{scene_number:02d}_{shot_number:02d}.png"
    video_path = f"plots/storybooks/{project_id}/97_shots/scene_{scene_number:02d}_shot_{shot_number:02d}/video_final_{scene_number:02d}_{shot_number:02d}.mp4"

    # Получаем данные персонажей из LLM результата (нормализованное сопоставление по имени)
    # LLM иногда возвращает некорректный тип (например, float вместо списка) — нормализуем.
    shot_characters = []
    char_index = { (c.get("name") or "").strip().lower(): c for c in characters_data }
    raw_characters = llm_result.get("characters", [])
    if isinstance(raw_characters, list):
        normalized_characters = raw_characters
    elif isinstance(raw_characters, str):
        normalized_characters = [raw_characters]
    elif isinstance(raw_characters, dict):
        name = raw_characters.get("name")
        if isinstance(name, str) and name.strip():
            normalized_characters = [name]
        else:
            raise ValueError(
                "Некорректный dict в llm_result.characters: ожидается ключ 'name' с непустой строкой"
            )
    else:
        raise ValueError(
            f"Некорректный тип llm_result.characters: ожидается list|str|dict(name), получен {type(raw_characters).__name__}"
        )

    for char_name in normalized_characters:
        key = (char_name or "").strip().lower()
        if key in char_index:
            shot_characters.append(char_index[key])
        else:
            # Поиск по частичному совпадению (для случаев вроде "Мышка-норушка" vs "Мышка")
            for char_key, char_data in char_index.items():
                if char_key in key or key in char_key:
                    shot_characters.append(char_data)
                    break

    # Fallback: если LLM не вернул список персонажей, попробуем извлечь их из текста
    # (включая storyboard.description текущего шота — это самый надёжный якорь для "кто в кадре").
    if not shot_characters and not _is_bs:
        combined_text = " ".join([
            str(llm_result.get("english_prompt", "")) or "",
            str(llm_result.get("initial_state_summary", "")) or "",
            str(llm_result.get("video_prompt", "")) or "",
            str(shot_description or "") or "",
        ]).lower()
        for char in characters_data:
            name = (char.get("name") or "").strip()
            if not name:
                continue
            # Поиск по основе имени (убираем суффиксы и префиксы)
            name_lower = name.lower()
            name_tokens = [token.strip() for token in name_lower.replace('-', ' ').split() if token.strip()]
            
            # Проверяем прямое вхождение или по токенам имени
            found = False
            if name_lower in combined_text:
                found = True
            else:
                # Ищем все токены имени в тексте
                if name_tokens and all(token in combined_text for token in name_tokens):
                    found = True
            
            if found:
                # Избегаем дублей по имени
                if not any((c.get("name") or "").strip().lower() == name_lower for c in shot_characters):
                    shot_characters.append(char)

    if _is_bs:
        shot_characters = []
    
    # ОГРАНИЧЕНИЕ: максимум 5 персонажей для character consistency
    MAX_CHARACTERS = 5
    if len(shot_characters) > MAX_CHARACTERS:
        logger.warning(f"⚠️ Превышено ограничение персонажей: {len(shot_characters)} > {MAX_CHARACTERS}. Обрезаем до {MAX_CHARACTERS}.")
        shot_characters = shot_characters[:MAX_CHARACTERS]
        logger.info(f"✂️ Персонажи после обрезки: {[c.get('name', '') for c in shot_characters]}")
    
    # Получаем данные локации ПРИОРИТЕТНО из location_time сценария, fallback на LLM
    shot_locations = []
    location_index_by_name: Dict[str, Dict[str, Any]] = {
        str((loc or {}).get("name") or "").strip().lower(): loc
        for loc in (locations_data or [])
        if str((loc or {}).get("name") or "").strip()
    }

    def _resolve_location_reference_path(loc_obj: Optional[Dict[str, Any]]) -> str:
        """
        Возвращает референс локации:
        1) собственный reference_image_path;
        2) если пусто — reference_image_path ближайшего parent_location_name.
        """
        if not isinstance(loc_obj, dict):
            return ""

        direct = str(loc_obj.get("reference_image_path") or "").strip()
        if direct:
            return direct

        visited = set()
        parent_name = str(loc_obj.get("parent_location_name") or "").strip()
        hops = 0
        while parent_name and hops < 6:
            key = parent_name.lower()
            if key in visited:
                break
            visited.add(key)
            parent = location_index_by_name.get(key) or {}
            parent_ref = str(parent.get("reference_image_path") or "").strip()
            if parent_ref:
                return parent_ref
            parent_name = str(parent.get("parent_location_name") or "").strip()
            hops += 1
        return ""

    def _sanitize_single_line_llm_value(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"<think>.*?</think>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"</?think>", " ", text, flags=re.IGNORECASE)
        text = extract_json_from_markdown(text).strip()
        lines = [line.strip(" \"'") for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        return lines[-1].strip()
    
    # ПРИОРИТЕТ: если каноническая локация уже известна, не тратим ещё один LLM-проход
    # на разбор location_time.
    location_name_from_script = ""
    if location_canon_name:
        logger.info(
            "ℹ️ LOCATION PARSING SKIP: используем canonical location '%s' без дополнительного LLM parsing",
            location_canon_name,
        )
    elif location_time:
        # Интеллектуально парсим сложную location_time через LLM
        try:
            complex_parsing_prompt = f"""Проанализируй location_time из сценария и определи ПРАВИЛЬНУЮ локацию для ЭТОГО КОНКРЕТНОГО кадра.

LOCATION_TIME: "{location_time}"

КОНТЕКСТ КАДРА (КРИТИЧЕСКИ ВАЖНО):
- scene_action: "{scene_action}"
- shot_description: "{shot_description}"

ПРАВИЛА АНАЛИЗА:
1. **ПРИОРИТЕТ #1: shot_description** - если в описании кадра явно упоминается конкретная локация - используй её
2. **ПАРАЛЛЕЛЬНЫЙ МОНТАЖ**: Если location_time содержит несколько локаций ("БУНКЕР / КАНЬОН", "КУХНЯ / ПРИХОЖАЯ"):
   - ОБЯЗАТЕЛЬНО проанализируй shot_description
   - Определи, ГДЕ происходит действие ЭТОГО кадра
   - Выбери СООТВЕТСТВУЮЩУЮ локацию
3. **FLASHBACK**: Если есть "FLASHBACK" или "ФЛЕШБЭК" - игнорируй, это вспомогательная метка
4. **ОЧИСТКА**: Убери технические префиксы "ИНТ.", "ЭКСТ.", "ЭКС.", "- ДЕНЬ", "- ВЕЧЕР", "- НОЧЬ"

АЛГОРИТМ:
- Если location_time содержит "/" (несколько зон) → определи по shot_description, В КАКОЙ ИМЕННО зоне происходит действие ЭТОГО кадра
- Сопоставь ключевые слова/действие из shot_description с названиями зон в location_time
- Если location_time содержит "ПАРАЛЛЕЛЬНЫЙ МОНТАЖ" → выбери ту зону, которая соответствует действию/персонажу из shot_description
- Если однозначная зона → верни её. Если неоднозначно → верни наиболее вероятную по контексту действия

ЗАДАЧА: Определи ПРАВИЛЬНУЮ локацию для ЭТОГО КОНКРЕТНОГО кадра на основе shot_description.

ВЕРНИ ТОЛЬКО НАЗВАНИЕ ЛОКАЦИИ (без префиксов, времени, скобок):"""

            resp = call_openai_api(
                prompt=complex_parsing_prompt,
                system_prompt="Ты эксперт по парсингу сценариев. Определи основную локацию из текста сценария.",
                model=model_ultimate,
                max_tokens=50,
                temperature=0.1
            )
            
            location_name_from_script = _sanitize_single_line_llm_value(resp)
            logger.info(f"🎬 COMPLEX PARSING: '{location_time}' → '{location_name_from_script}'")
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга сложной location_time: {e}")
    
    # Выбираем приоритетное название: canonical > script-derived > llm guess.
    location_name = (
        _sanitize_single_line_llm_value(location_canon_name)
        or location_name_from_script
        or _sanitize_single_line_llm_value(llm_result.get("location", ""))
    )
    
    # Логируем что используем для отладки
    available_location_names = [loc.get("name", "") for loc in locations_data]
    logger.info(
        f"🔍 LOCATION ANALYSIS: Используем локацию='{location_name}' "
        f"(canon: '{location_canon_name}', script: '{location_name_from_script}', llm: '{llm_result.get('location', '')}'), "
        f"доступные={available_location_names}"
    )
    
    if location_name:
        def _normalize_location_name(name: str) -> str:
            s = (name or "").strip().lower()
            # убрать метки в скобках: (flashback), (флешбэк) и т.п.
            s = re.sub(r"\([^)]*\)", " ", s)
            # убрать ключевые слова флешбэка
            s = re.sub(r"\bflashback\b", " ", s)
            s = re.sub(r"\bфлешб[еэ]к\b", " ", s)
            # убрать инт/экст и время суток
            s = re.sub(r"\b(инт\.?|экст\.?|экс\.?)\b", " ", s)
            s = re.sub(r"\b(день|ночь|вечер|утро)\b", " ", s)
            # убрать разделители
            s = re.sub(r"[_\-–—/]", " ", s)
            s = re.sub(r"\s+", " ", s).strip()
            return s

        def _pick_best_location(cands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if not cands:
                return None
            # предпочитаем локацию с location_sheet_instruction (если есть), иначе с более подробным описанием
            def score(loc: Dict[str, Any]) -> int:
                sc = 0
                if loc.get("location_sheet_instruction"):
                    sc += 1000
                sc += len((loc.get("description") or "").strip())
                sc += 10 * len(loc.get("key_objects") or [])
                return sc
            return sorted(cands, key=score, reverse=True)[0]

        key_loc = (location_name or "").strip().lower()
        key_loc_norm = _normalize_location_name(location_name)
        
        # Сначала пытаемся точное совпадение
        loc_index = { (l.get("name") or "").strip().lower(): l for l in locations_data }
        if key_loc in loc_index:
            shot_locations.append(loc_index[key_loc])
        else:
            # Пробуем нормализованное совпадение по имени (игнорируем FLASHBACK/скобки/время суток)
            norm_index: Dict[str, List[Dict[str, Any]]] = {}
            for l in locations_data:
                nm = (l.get("name") or "").strip()
                kn = _normalize_location_name(nm)
                if not kn:
                    continue
                norm_index.setdefault(kn, []).append(l)
            if key_loc_norm and key_loc_norm in norm_index:
                chosen = _pick_best_location(norm_index[key_loc_norm])
                if chosen:
                    shot_locations.append(chosen)
                    logger.info(f"🔍 LOCATION NORMALIZED MATCH: '{location_name}' → '{chosen.get('name','')}'")
        # Если уже нашли — не продолжаем
        if shot_locations:
            pass
        else:
            # Второй LLM-проход: выбираем ближайшую КАНОНИЧЕСКУЮ локацию строго из available_locations.
            try:
                available_locations = []
                for loc in locations_data or []:
                    available_locations.append(
                        {
                            "name": (loc.get("name") or "").strip(),
                            "description": (loc.get("description") or "").strip()[:500],
                            "key_objects": list(loc.get("key_objects") or [])[:12],
                        }
                    )

                remap_prompt = (
                    "Выбери наиболее подходящую КАНОНИЧЕСКУЮ локацию для текущего кадра.\n"
                    "Ограничение: ответ должен быть строго одним из available_locations[].name или null.\n\n"
                    f"requested_location: {json.dumps(location_name, ensure_ascii=False)}\n"
                    f"location_time: {json.dumps(location_time, ensure_ascii=False)}\n"
                    f"scene_action: {json.dumps(scene_action, ensure_ascii=False)}\n"
                    f"shot_description: {json.dumps(shot_description, ensure_ascii=False)}\n"
                    f"available_locations: {json.dumps(available_locations, ensure_ascii=False)}\n\n"
                    "Верни ТОЛЬКО JSON:\n"
                    "{\"canonical_location_name\": \"<one_of_available_or_null>\", \"reason\": \"short\"}"
                )

                remap_resp = call_openai_api(
                    prompt=remap_prompt,
                    system_prompt=(
                        "Ты валидатор локаций. Разрешено выбирать только из provided available_locations. "
                        "Не выдумывай новые названия."
                    ),
                    model=model_ultimate,
                    max_tokens=300,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )

                remap_obj = {}
                try:
                    remap_obj = json.loads(extract_json_from_markdown(remap_resp))
                except Exception:
                    remap_obj = {}

                remap_name = _sanitize_single_line_llm_value(
                    (remap_obj or {}).get("canonical_location_name")
                )
                if remap_name.lower() == "null":
                    remap_name = ""

                remap_index = {
                    (l.get("name") or "").strip().lower(): l
                    for l in (locations_data or [])
                }
                mapped = remap_index.get(remap_name.strip().lower()) if remap_name else None
                if mapped:
                    shot_locations.append(mapped)
                    logger.info(
                        "🔁 LOCATION LLM REMAP: requested='%s' -> canonical='%s' (scene=%s shot=%s)",
                        location_name,
                        mapped.get("name", ""),
                        scene_number,
                        shot_number,
                    )
            except Exception as e:
                logger.error(
                    "❌ LOCATION LLM REMAP ERROR: scene=%s shot=%s requested='%s' error=%s",
                    scene_number,
                    shot_number,
                    location_name,
                    e,
                )

            if not shot_locations:
                logger.error(
                    "❌ LOCATION NOT FOUND WITHOUT SMART MATCH: scene=%s shot=%s requested='%s' canon='%s' available=%s",
                    scene_number,
                    shot_number,
                    location_name,
                    location_canon_name,
                    [l.get("name", "") for l in locations_data],
                )
    else:
        logger.warning(f"⚠️ NO LOCATION FROM LLM: LLM не вернул поле 'location'")

    if shot_type == "end" and not shot_locations:
        # END всегда должен сохранять локацию START того же шота.
        # Если локальный резолвер не нашёл локацию, поднимаем её из START item в shots.json.
        try:
            shots_path = f"plots/storybooks/{project_id}/97_shots/shots.json"
            if os.path.exists(shots_path):
                with open(shots_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                items = payload.get("items", []) if isinstance(payload, dict) else []
                start_item = None
                for candidate in items:
                    if (
                        int(candidate.get("scene_number", 0) or 0) == int(scene_number)
                        and int(candidate.get("shot_number", 0) or 0) == int(shot_number)
                        and str(candidate.get("shot_type", "")).strip().lower() == "start"
                    ):
                        start_item = candidate
                        break

                if isinstance(start_item, dict):
                    start_locs = start_item.get("locations")
                    if isinstance(start_locs, list) and start_locs:
                        shot_locations = [dict(loc) for loc in start_locs if isinstance(loc, dict)]

                    if not shot_locations:
                        # Fallback: если в START metadata локация отсутствует, восстановим по location-ref START.
                        start_refs = start_item.get("reference_image_paths") or []
                        start_location_ref = ""
                        for ref in start_refs:
                            rr = str(ref or "").strip()
                            if rr.startswith("/references/locations/"):
                                start_location_ref = rr
                                break
                        if start_location_ref:
                            for loc in (locations_data or []):
                                if _resolve_location_reference_path(loc) == start_location_ref:
                                    shot_locations = [loc]
                                    break

                if shot_locations:
                    logger.info(
                        "🔒 END LOCATION FALLBACK: inherited START location for scene=%s shot=%s -> %s",
                        scene_number,
                        shot_number,
                        [l.get("name", "") for l in shot_locations],
                    )
        except Exception as e:
            logger.warning(
                "⚠️ END LOCATION FALLBACK failed for scene=%s shot=%s: %s",
                scene_number,
                shot_number,
                e,
            )

    if _is_bs:
        shot_locations = []
    
    # # КРИТИЧЕСКИЙ FALLBACK: если локация не определена, берём первую из доступных
    # if not shot_locations and locations_data:
    #     fallback_location = locations_data[0]
    #     shot_locations.append(fallback_location)
    #     logger.info(f"🆘 FALLBACK LOCATION: Принудительно используется '{fallback_location.get('name', '')}' как локация по умолчанию")
    
    # Собираем reference_image_paths
    reference_paths = []
    
    # Для end кадров добавляем соответствующий start кадр ПЕРВЫМ в референсы
    if shot_type == "end":
        start_image_path = f"plots/storybooks/{project_id}/97_shots/scene_{scene_number:02d}_shot_{shot_number:02d}/img_final_start_{scene_number:02d}_{shot_number:02d}.png"
        reference_paths.append(start_image_path)
    
    # Для start кадров проверяем, есть ли continuity reference в LLM результате
    elif shot_type == "start":
        llm_reference_paths = llm_result.get('reference_image_paths', [])
        if llm_reference_paths:
            # Если первый путь содержит "/97_shots/" - это continuity reference
            first_ref = llm_reference_paths[0] if llm_reference_paths else ""
            if "/97_shots/" in first_ref and first_ref.endswith(".png"):
                reference_paths.append(first_ref)
    
    # Добавляем character references
    for char in shot_characters:
        ref_path = char.get("reference_image_path")
        if ref_path and char.get("type") != "continuity":  # Исключаем continuity, он уже добавлен
            reference_paths.append(ref_path)
    
    # Добавляем location reference В КОНЦЕ (для smart background selection)
    for loc in shot_locations:
        ref_path = _resolve_location_reference_path(loc)
        if ref_path:
            reference_paths.append(ref_path)
    
    # ОГРАНИЧЕНИЕ: всего 10 дополнительных изображений (персонажи + прочие референсы)
    # ВАЖНО: сохраняем порядок референсов: continuity/start → characters → location
    MAX_TOTAL_IMAGES = 10
    char_ref_paths = set(
        (c.get("reference_image_path") or "").strip()
        for c in shot_characters
        if c.get("type") != "continuity" and (c.get("reference_image_path") or "").strip()
    )
    char_refs_in_order = [r for r in reference_paths if r in char_ref_paths]

    # Определяем continuity/start ref (как правило первый /97_shots/*.png)
    continuity_ref = None
    if reference_paths:
        first_ref = reference_paths[0] or ""
        if "/97_shots/" in first_ref and first_ref.endswith(".png"):
            continuity_ref = first_ref

    # Определяем location ref (в идеале последний референс локации)
    loc_ref_paths = set(_resolve_location_reference_path(l) for l in shot_locations if _resolve_location_reference_path(l))
    location_ref = None
    if loc_ref_paths:
        for r in reversed(reference_paths):
            if r in loc_ref_paths:
                location_ref = r
                break

    def _should_include_location_ref_for_end(
        loc_ref: str,
        llm: Dict[str, Any],
        locs: List[Dict[str, Any]]
    ) -> bool:
        """
        Универсальная защита от дрейфа стиля/локации из multi-view location sheet в END-кадрах.
        Если у нас есть continuity (image 1) и локация — это 4-панельный sheet, то по умолчанию
        НЕ добавляем location ref, если кадр не требует расширения/достройки окружения.
        """
        if not loc_ref:
            return False
        loc_obj = next((l for l in locs if (l.get("reference_image_path") or "").strip() == loc_ref), None) or {}
        is_sheet = bool((loc_obj.get("location_sheet_instruction") or "").strip())
        if not is_sheet:
            return True  # обычная локация: оставляем как раньше

        # Для sheet добавляем только если действительно нужно достраивать пространство
        try:
            ratio = llm.get("subject_scale_ratio", 1.0)
            ratio_f = float(ratio) if ratio is not None else 1.0
        except Exception:
            ratio_f = 1.0
        try:
            framing_delta = int(llm.get("framing_delta_percent", 0) or 0)
        except Exception:
            framing_delta = 0

        text_blob = " ".join([
            str(llm.get("english_prompt", "") or ""),
            str(llm.get("spatial_changes_from_start", "") or ""),
            str(llm.get("final_spatial_composition", "") or ""),
        ]).lower()
        needs_expand_keywords = any(k in text_blob for k in [
            "expand background", "background field of view", "reveal", "wider view", "zoom out",
            "show more", "widen", "more of the room", "ceiling", "floor",
            "расшир", "показать больше", "шире", "открыть", "раскрыть", "увидеть больше", "потолок", "пол"
        ])

        needs_expand_numeric = (ratio_f < 0.95) or (framing_delta >= 10)
        return bool(needs_expand_keywords or needs_expand_numeric)

    other_refs = []
    if continuity_ref:
        other_refs.append(continuity_ref)

    # Для END: location sheet добавляем только если нужно расширять/достраивать окружение
    if location_ref and location_ref != continuity_ref:
        if shot_type == "end" and continuity_ref:
            if _should_include_location_ref_for_end(location_ref, llm_result, shot_locations):
                other_refs.append(location_ref)
        else:
            other_refs.append(location_ref)

    num_character_refs = len(char_refs_in_order)
    max_other_refs = max(MAX_TOTAL_IMAGES - num_character_refs, 0)
    if len(other_refs) > max_other_refs:
        logger.warning(
            f"⚠️ Превышено ограничение референсов: total>{MAX_TOTAL_IMAGES}. "
            f"Персонажей: {num_character_refs}, лимит для остальных: {max_other_refs}"
        )
        # Всегда сохраняем continuity/start если он есть
        trimmed_other = []
        if continuity_ref and max_other_refs >= 1:
            trimmed_other.append(continuity_ref)
        # location добавляем только если остался слот
        if location_ref and location_ref != continuity_ref and len(trimmed_other) < max_other_refs:
            trimmed_other.append(location_ref)
        other_refs = trimmed_other

    include_location_ref = bool(location_ref and location_ref in other_refs)
    char_primary = _character_reference_should_be_primary_for_img2img(
        shot_frame_spec=shot_frame_spec,
        shot_type=shot_type,
        continuity_ref=continuity_ref,
        char_refs_in_order=char_refs_in_order,
        location_ref=location_ref,
        shot_characters=shot_characters,
    )
    if char_primary and shot_type == "start" and not continuity_ref:
        logger.info(
            "🎯 REF ORDER: character ref(s) before location (scene_mode from shot_frame_spec → primary img2img input = character)"
        )

    # Финальная сборка: image 1 в API = первый элемент (для img2img — главный вход).
    # Для START без continuity: при single_subject/ensemble/object_focus+primary=character — персонаж первым.
    reference_paths: List[str] = []
    if continuity_ref:
        reference_paths.append(continuity_ref)
        for r in char_refs_in_order:
            if r != continuity_ref:
                reference_paths.append(r)
        if include_location_ref and location_ref and location_ref != continuity_ref:
            reference_paths.append(location_ref)
    elif shot_type == "start":
        if char_primary and char_refs_in_order and include_location_ref:
            reference_paths.extend(char_refs_in_order)
            reference_paths.append(location_ref)
        elif char_primary and char_refs_in_order and not include_location_ref:
            reference_paths.extend(char_refs_in_order)
        elif include_location_ref:
            reference_paths.append(location_ref)
            reference_paths.extend(char_refs_in_order)
        else:
            reference_paths.extend(char_refs_in_order)
    else:
        if other_refs and other_refs[0]:
            reference_paths.append(other_refs[0])
        reference_paths.extend(char_refs_in_order)
        if include_location_ref and location_ref and location_ref != continuity_ref:
            reference_paths.append(location_ref)

    # Дедупликация референсов (с сохранением порядка) — защищает от случаев,
    # когда LLM возвращает локацию/реф дважды (как в scene_02_shot_02 start).
    _seen = set()
    _deduped: List[str] = []
    for r in reference_paths:
        rr = (r or "").strip()
        if not rr:
            continue
        if rr in _seen:
            continue
        _seen.add(rr)
        _deduped.append(r)
    reference_paths = _deduped

    if _is_bs:
        if shot_type == "start":
            reference_paths = []
        elif shot_type == "end":
            reference_paths = [
                p for p in reference_paths if "/97_shots/" in str(p) and str(p).endswith(".png")
            ][:1]

    # Жёсткая страховка на общий лимит
    if len(reference_paths) > MAX_TOTAL_IMAGES:
        reference_paths = reference_paths[:MAX_TOTAL_IMAGES]
    logger.info(f"🔢 Итоговые референсы: {len(reference_paths)}/{MAX_TOTAL_IMAGES} (characters={num_character_refs}, other={len(other_refs)})")
    
    # Сформируем явную инструкцию о РОЛЯХ референсов (для Nano Banana API)
    reference_roles_instruction = ""
    if reference_paths:
        # Карты путь→имя для быстрого определения роли
        loc_path_to_name: Dict[str, str] = {}
        for loc in shot_locations:
            resolved_ref = _resolve_location_reference_path(loc)
            if resolved_ref and resolved_ref not in loc_path_to_name:
                loc_path_to_name[resolved_ref] = (loc.get("name") or "").strip()
        # Карта путь→объект локации (для multi-view sheet и других спец-инструкций)
        loc_path_to_loc: Dict[str, Dict[str, Any]] = {}
        for loc in shot_locations:
            resolved_ref = _resolve_location_reference_path(loc)
            if resolved_ref and resolved_ref not in loc_path_to_loc:
                loc_path_to_loc[resolved_ref] = loc
        char_path_to_name = { (c.get("reference_image_path") or "").strip(): (c.get("name") or "").strip() for c in shot_characters }
        # Карта для типов (continuity, character, location)
        char_path_to_type = { (c.get("reference_image_path") or "").strip(): c.get("type", "character") for c in shot_characters }
        role_entries: List[str] = []
        has_location_multiview_sheet = False
        has_continuity_role = False
        for idx, ref in enumerate(reference_paths, start=1):
            label = ""
            name = ""
            if ref in char_path_to_type and char_path_to_type[ref] == "continuity":
                # Continuity reference is the primary source for surviving background geometry.
                has_continuity_role = True
                location_name = ""
                if shot_locations:
                    location_name = shot_locations[0].get("name", "")
                label = (
                    f"image {idx} as continuity reference (pose lineage+lighting, одежда, прическа, предметы, existing background geometry and anchor placement); background source — {location_name}"
                    if location_name else
                    f"image {idx} as continuity reference (pose lineage+lighting, одежда, прическа, предметы, existing background geometry and anchor placement)"
                )
            elif ref in loc_path_to_name and loc_path_to_name[ref]:
                name = loc_path_to_name[ref]
                # Если это location sheet (multi-view), явно говорим модели как читать 4-панельный реф
                loc_obj = loc_path_to_loc.get(ref, {}) or {}
                if (loc_obj.get("location_sheet_instruction") or "").strip():
                    has_location_multiview_sheet = True
                    # IMPORTANT: avoid the word "panel/sheet" — it often triggers collage outputs.
                    label = (
                        f"image {idx} as location — {name} "
                        f"(multi-view reference: select ONE view that matches this shot's camera plan / POV / zone; "
                        f"use it to ground geometry+palette; do not output split-screen unless storyboard explicitly requires it)"
                    )
                else:
                    if has_continuity_role:
                        label = (
                            f"image {idx} as location support — {name} "
                            f"(materials+palette+off-frame continuation only; do not replace image 1 background geometry or anchor placement)"
                        )
                    else:
                        label = f"image {idx} as location — {name} (layout+lighting+palette)"
            elif ref in char_path_to_name and char_path_to_name[ref]:
                name = char_path_to_name[ref]
                # Персонажный референс: приоритет идентичности над всем остальным
                label = (
                    f"image {idx} as character — {name} "
                    f"(идентичность: лицо, глаза, пропорции; стиль проекта)"
                )
            elif "/97_shots/" in ref and ref.endswith(".png"):
                # Fallback: сгенерированный кадр как continuity reference - ИСТОЧНИК ФОНА
                location_name = ""
                if shot_locations:
                    location_name = shot_locations[0].get("name", "")
                label = (
                    f"image {idx} as continuity reference (pose+composition+lighting, одежда, прическа, предметы); background source — {location_name}"
                    if location_name else
                    f"image {idx} as continuity reference (pose+composition+lighting, одежда, прическа, предметы)"
                )
            else:
                label = f"image {idx} as style reference (color grading+texture)"
            role_entries.append(label)
        if role_entries:
            roles_text = "; ".join(role_entries)
            # Компактная, “аспектная” инструкция (лучше следует best-practices и меньше засоряет команду)
            extra = ""
            if has_location_multiview_sheet:
                extra = (
                    " Локация может содержать несколько видов; выход — один кадр (не коллаж), если это явно не запрошено в storyboard. "
                    "Используй один вид для геометрии, остальные — для деталей. "
                    "Сохранён стиль continuity изображения."
                )
            if has_continuity_role:
                extra = (
                    (extra or "" + " " if extra else "")
                    + " Image 1 как continuity: сохранить ту же геометрию фона, архитектурный каркас и относительное положение узнаваемых фоновых якорей, "
                      "если их не исключает tighter crop или явно не меняет `added`/`removed`. "
                      "Локация может только дополнять материалы, свет, палитру и выход за пределы кропа, но не заменять layout фона и не переставлять якоря."
                )
            reference_roles_instruction = (
                f"Опорные образы: {roles_text}."
                + (f" {extra}" if extra else "")
                + " Сохранить идентичность, перспективу, освещение и естественные тени; избегать отклонения стиля."
            )

    if _is_bs and shot_type == "end":
        if language == "ru":
            reference_roles_instruction = (
                "Опорные образы: image 1 as continuity — сведи к полностью чёрному кадру (#000000), без деталей; "
                "не сохраняй геометрию фона и не добавляй референсы локации/персонажа."
            )
        else:
            reference_roles_instruction = (
                "Reference anchors: image 1 as continuity — reduce to a fully black (#000000) frame with no detail; "
                "do not preserve background geometry; do not add location/character references."
            )
    
    # DEBUG: Проверяем reference_roles_instruction перед добавлением
    logger.info(f"🔍 DEBUG: reference_roles_instruction = '{reference_roles_instruction}'")
    logger.info(f"🔍 DEBUG: reference_paths = {reference_paths}")

    # --- Нормализация промптов (детерминированно, без LLM) ---
    def _normalize_start_prompt_prefix(prompt: str, has_continuity_ref: bool) -> str:
        p = (prompt or "").strip()
        if not p:
            return p

        lowered = p.lower()

        def _strip_known_prefix(text: str) -> str:
            candidate = text.strip()
            lowered_candidate = candidate.lower()
            for prefix in sorted(all_edit_prefixes, key=len, reverse=True):
                if lowered_candidate.startswith(prefix):
                    return candidate[len(prefix):].lstrip(" :-—")
            for prefix in sorted(all_create_prefixes, key=len, reverse=True):
                if lowered_candidate.startswith(prefix):
                    return candidate[len(prefix):].lstrip(" :-—")
            return candidate

        body = _strip_known_prefix(p)
        if has_continuity_ref:
            return f"{target_edit_prefix} {body}".strip()

        if lowered.startswith(target_create_prefix.lower()):
            return p
        return f"{target_create_prefix} {body}".strip()

    def _needs_text_in_image(*texts: str) -> bool:
        blob = " ".join([t for t in texts if t]).lower()
        # ключевые слова, когда в кадре вероятно нужен читаемый текст/интерфейс
        keywords = [
            "subtitle", "caption", "title", "headline", "label",
            "sign", "poster", "billboard", "banner", "logo",  # logo спорно, но лучше не блокировать "text" если это явно нужно
            "menu", "price", "receipt",
            "screen", "display", "monitor", "tv", "tablet", "smartphone", "phone",
            "ui", "interface", "dashboard", "infographic", "diagram", "chart", "graph",
            "text on", "words", "written", "lettering",
            # русские
            "надпись", "текст", "субтитр", "заголовок", "табличк", "вывеск", "экран", "планшет", "телефон", "интерфейс", "дашборд", "инфографик", "диаграмм", "график",
        ]
        return any(k in blob for k in keywords)

    def _normalize_negative_prompt(
        neg: str,
        needs_text: bool,
        *,
        continuity_background_lock: bool = False,
    ) -> str:
        n = (neg or "").strip()
        if not n:
            return n
        # разбиваем по запятым, чистим пробелы
        parts = [p.strip() for p in n.split(",") if p.strip()]
        lower_parts = [p.lower() for p in parts]

        def _remove_exact(token: str):
            nonlocal parts, lower_parts
            keep = [(p, lp) for p, lp in zip(parts, lower_parts) if lp != token]
            parts = [p for p, _ in keep]
            lower_parts = [lp for _, lp in keep]

        def _ensure(token: str):
            nonlocal parts, lower_parts
            if token.lower() not in lower_parts:
                parts.append(token)
                lower_parts.append(token.lower())

        if needs_text:
            # НЕ блокируем весь текст, но блокируем нежелательный/случайный
            _remove_exact(prompt_language_tokens["text"].lower())
            _ensure(prompt_language_tokens["unwanted_text"])
            _ensure(prompt_language_tokens["random_text"])
            _ensure(prompt_language_tokens["watermark"])
            _ensure(prompt_language_tokens["logo"])
            # если нужно: запрещаем длинные полотна текста, но не UI-лейблы
            _ensure(prompt_language_tokens["no_long_text"])
        else:
            # обычный режим: блокируем текст как артефакт
            _remove_exact(prompt_language_tokens["unwanted_text"].lower())
            _remove_exact(prompt_language_tokens["random_text"].lower())
            _remove_exact(prompt_language_tokens["no_long_text"].lower())
            _ensure(prompt_language_tokens["text"])
            _ensure(prompt_language_tokens["watermark"])

        # Universal: never output multi-panel / split-screen / collage unless explicitly required by storyboard.
        # Keeping it in negative prompt reduces accidental "sheet/grid" outputs for BOTH start and end frames.
        multipanel_tokens = prompt_language_tokens["multipanel"]
        for t in multipanel_tokens:
            _ensure(t)

        if continuity_background_lock:
            for t in prompt_language_tokens["continuity_lock"]:
                _ensure(t)

        return ", ".join(parts)

    def _strip_multiview_meta_from_prompt(prompt: str) -> str:
        """
        The generator already provides roles/instructions about reference images.
        If the model also repeats '4 views / panels / sheet' meta inside the core prompt,
        it often triggers a collage output. We strip those meta lines conservatively.
        """
        p = (prompt or "").strip()
        if not p:
            return p
        lines = [ln.strip() for ln in p.splitlines()]
        drop_keys = [
            "4 вида",
            "четыр",
            "multi-view",
            "4 views",
            "panel",
            "панел",
            "sheet",
            "grid",
            "не разделяйте изображение на панели",
            "do not split the output",
            "select the single panel",
        ]
        kept: List[str] = []
        for ln in lines:
            lnl = ln.lower()
            if any(k in lnl for k in drop_keys) and ("опорн" in lnl or "reference" in lnl or "location" in lnl):
                continue
            kept.append(ln)
        out = "\n".join([x for x in kept if x]).strip()
        return out
    
    # Создаем элемент в формате items.json + расширенные поля
    # Определяем наличие continuity ref для start: обычно это первый референс из /97_shots/
    has_continuity_ref = False
    if shot_type == "start" and reference_paths:
        first_ref = reference_paths[0] or ""
        has_continuity_ref = ("/97_shots/" in first_ref and first_ref.endswith(".png"))

    raw_english_prompt = (llm_result.get("english_prompt", "") or "").strip()
    normalized_english_prompt = _strip_multiview_meta_from_prompt(raw_english_prompt)
    if shot_type == "start":
        normalized_english_prompt = _normalize_start_prompt_prefix(normalized_english_prompt, has_continuity_ref)

    if _is_bs:
        create_pf = prompt_create_prefixes.get(language, prompt_create_prefixes["en"])
        edit_pf = prompt_edit_prefixes.get(language, prompt_edit_prefixes["en"])
        vs_tail = ""
        if (visual_style or "").strip():
            if language == "ru":
                vs_tail = (
                    f"\n\nВизуальный стиль серии (дисциплина панели; кадр остаётся чёрным): "
                    f"{(visual_style or '').strip()}"
                )
            else:
                vs_tail = (
                    f"\n\nSeries visual style (panel discipline; frame stays black): "
                    f"{(visual_style or '').strip()}"
                )
        if shot_type == "start":
            if language == "ru":
                body = (
                    "кадр: ровное полное чёрное полотно (#000000) как единая панель; без силуэтов, без среды, "
                    "без градиентов, без виньетки, без плёночного зерна, без текста и UI. "
                    "Black screen — не изображай персонажей и локацию."
                )
            else:
                body = (
                    "a flat pure black (#000000) single panel; no silhouettes, no environment, no gradients, "
                    "no vignette, no film grain, no text, no UI. Black screen — do not depict characters or locations."
                )
            normalized_english_prompt = f"{create_pf} {body}{vs_tail}".strip()
        else:
            if language == "ru":
                body = (
                    "Преврати кадр в ровное полное чёрное (#000000): убери любые объекты, персонажей, свет, "
                    "текстуры, градиенты и шум; визуальный нуль."
                )
            else:
                body = (
                    "Turn the frame into flat pure black (#000000): remove any objects, characters, light, textures, "
                    "gradients, and noise — visual null."
                )
            normalized_english_prompt = f"{edit_pf} {body}{vs_tail}".strip()
    elif (visual_style or "").strip():
        vs = (visual_style or "").strip()
        if language == "ru":
            normalized_english_prompt = (
                f"{normalized_english_prompt}\n\nВизуальный стиль проекта (анкор, без дрейфа стиля): {vs}"
            ).strip()
        else:
            normalized_english_prompt = (
                f"{normalized_english_prompt}\n\nProject visual style anchor (match treatment; avoid style drift): {vs}"
            ).strip()

    # Visibility-pass для аксессуаров/одежды: что должно быть видно/скрыто в этом ракурсе.
    shot_frame_spec_out = dict(shot_frame_spec) if isinstance(shot_frame_spec, dict) else {}
    if _is_bs:
        accessory_visibility = {"must_show_additions": [], "must_not_show_additions": [], "negative_prompt_tokens": []}
    else:
        accessory_visibility = _infer_accessory_visibility_via_llm(
            shot_type=shot_type,
            shot_frame_spec=shot_frame_spec_out,
            llm_result=llm_result,
            shot_characters=shot_characters,
        )
    if shot_frame_spec_out:
        for fld, key in (("must_show", "must_show_additions"), ("must_not_show", "must_not_show_additions")):
            existing = shot_frame_spec_out.get(fld)
            existing_list = [str(x).strip() for x in existing] if isinstance(existing, list) else []
            seen = {x.lower() for x in existing_list if x}
            for v in accessory_visibility.get(key, []):
                vv = str(v).strip()
                if vv and vv.lower() not in seen:
                    existing_list.append(vv)
                    seen.add(vv.lower())
            shot_frame_spec_out[fld] = existing_list

    # Conditional negative prompt: разрешаем текст, если в кадре вероятно нужен UI/надписи
    raw_negative = llm_result.get(
        "negative_prompt",
        prompt_language_tokens["default_negative"],
    )
    extra_visibility_negative = ", ".join(accessory_visibility.get("negative_prompt_tokens", []))
    if extra_visibility_negative:
        raw_negative = f"{raw_negative}, {extra_visibility_negative}" if str(raw_negative).strip() else extra_visibility_negative
    needs_text = _needs_text_in_image(
        shot_description,
        scene_action,
        raw_english_prompt,
        llm_result.get("initial_state_summary", "") or "",
        llm_result.get("video_prompt", "") or "",
    )
    continuity_background_lock = bool(shot_type == "end" and continuity_ref and not _is_bs)
    normalized_negative = _normalize_negative_prompt(
        raw_negative,
        needs_text,
        continuity_background_lock=continuity_background_lock,
    )
    if _is_bs:
        if language == "ru":
            black_extra = (
                "свет, силуэты, градиенты, виньетка, зерно, детали окружения, персонажи, текст, "
                "фотореализм, кинематографический кадр, CGI"
            )
        else:
            black_extra = (
                "light, silhouettes, gradients, vignette, grain, environment detail, characters, text, "
                "photorealism, cinematic live-action look, CGI"
            )
        normalized_negative = (
            f"{normalized_negative}, {black_extra}" if (normalized_negative or "").strip() else black_extra
        )
    normalized_negative = merge_style_do_not_into_negative(normalized_negative, style_do_not_include)

    # NOTE: We intentionally do NOT post-fix the human-readable shot-size phrase in english_prompt.
    # Shot sizing must be handled upstream by storyboard camera_plan locks in technical/artistic prompts.

    shot_item = {
        # Базовые поля
        "project_id": project_id,
        "page_number": page_number,
        "scene_number": scene_number,
        "shot_number": shot_number,
        "shot_type": shot_type,
        "camera_plan": camera_plan,
        "timing": timing,
        "image_path": None,
        "english_prompt": (normalized_english_prompt + ("\n\n" + reference_roles_instruction if reference_roles_instruction else "")),
        "video_prompt": (llm_result.get("video_prompt", "").strip()),
        "negative_prompt": normalized_negative,
        "reference_image_paths": reference_paths,
        "width": 1920,
        "height": 1080,
        "true_cfg_scale": 7.5,
        "num_inference_steps": 30,
        "seed": seed if seed else None,
        "number": item_number,
        "output_path": output_path,
        "video_path": video_path,
        "characters": shot_characters,
        "locations": shot_locations,
        
        # Новые поля для улучшенной логики
        "should_use_prev_end_as_reference": llm_result.get("should_use_prev_end_as_reference", False),
        "continuity_score": llm_result.get("continuity_score", 5),
        "composition_stability": llm_result.get("composition_stability", "stable"),
        "spatial_changes_from_start": llm_result.get("spatial_changes_from_start", ""),
        "link_type": "independent",  # Будет обновлено позже в зависимости от логики
        "link_reasoning": llm_result.get("link_reasoning", ""),
        "extended_context_used": True,
        "narrative_position": llm_result.get("narrative_position", "основное развитие"),
        "scene_pacing": llm_result.get("scene_pacing", "средняя"),
        
        # Пространственные параметры для START кадров
        "camera_position": llm_result.get("camera_position", ""),
        "character_orientation": llm_result.get("character_orientation", ""),
        "spatial_composition": llm_result.get("spatial_composition", ""),
        "point_of_view": llm_result.get("point_of_view", "objective"),
        "initial_state_summary": llm_result.get("initial_state_summary", ""),
        
        # Финальные параметры для END кадров
        "final_shot_size": llm_result.get("final_shot_size", ""),
        "final_camera_angle": llm_result.get("final_camera_angle", ""),
        "final_lighting_style": llm_result.get("final_lighting_style", ""),
        "final_color_palette": llm_result.get("final_color_palette", ""),
        "final_camera_position": llm_result.get("final_camera_position", ""),
        "final_character_orientation": llm_result.get("final_character_orientation", ""),
        "final_spatial_composition": llm_result.get("final_spatial_composition", ""),
        "final_point_of_view": llm_result.get("final_point_of_view", ""),
        "camera_movement_completed": llm_result.get("camera_movement_completed", True),
        "next_shot_compatibility": llm_result.get("next_shot_compatibility", ""),
        
        # Количественная дельта фрейминга и реквизит
        "framing_delta_percent": llm_result.get("framing_delta_percent", 0),
        "subject_scale_ratio": llm_result.get("subject_scale_ratio", 1.0),
        "prop_continuity": llm_result.get("prop_continuity", {"kept": [], "removed": [], "added": [], "notes": ""}),
        # Новые финальные технические поля (если присутствуют)
        "final_camera_yaw_deg": llm_result.get("final_camera_yaw_deg", 0),
        "final_camera_pitch_deg": llm_result.get("final_camera_pitch_deg", 0),
        "final_subject_yaw_deg": llm_result.get("final_subject_yaw_deg", 0),
        "final_focus_target": llm_result.get("final_focus_target", ""),
        "final_depth_of_field": llm_result.get("final_depth_of_field", ""),
        "main_subject": llm_result.get("main_subject", ""),
        "final_depth_order": llm_result.get("final_depth_order", []),
        # Доп. поле: явные роли референсов (для downstream-API при необходимости)
        "reference_roles_instruction": reference_roles_instruction,
        # Internal source-of-truth metadata for downstream QA/runtime analysis.
        "_shot_frame_spec": shot_frame_spec_out or {},
        "_shot_frame_spec_cache_key": shot_frame_spec_cache_key or "",
        "_scene_continuity_facts": scene_continuity_facts or {},
    }

    logger.info(f"ℹ️ Пропускаем post-generation optimization для {shot_type.upper()}: source of truth уже зафиксирован upstream")
    return shot_item

def _sanitize_start_via_llm(
    start_result: Dict[str, Any],
    extended_context: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    LLM-санитайзер для START: жестко фиксирует T=0 текущего шота, убирает процессные/будущие элементы,
    усиливает negative и приоритизирует shot_description над сценовым action.
    Возвращает отредактированный JSON start_result.
    """
    system_prompt = (
        "Ты — санитайзер промптов. Приведи START-кадр к T=0 (статика текущего кадра).\n\n"
        "**ПРИОРИТЕТЫ ПРАВОК:**\n"
        "1. [P1] Масштабирование 1.0±0.02 → удалить\n"
        "2. [P1] T=0 статика → удалить процессы/будущее\n"
        "3. [P2] Границы кадра → удалить ссылки на соседние кадры\n"
        "4. [P3] Минимум остальных правок\n\n"
        "**[P1] ВАЛИДАЦИЯ МАСШТАБИРОВАНИЯ:**\n"
        "Удали команды с ratio ≈ 1.0:\n"
        "❌ 'Increase by 1.0x' / 'by 1x' / 'by 0.98x' / 'by 0.99x' / 'by 1.01x' / 'by 1.02x'\n"
        "✅ Сохрани: 'by 1.2x', 'by 0.8x'\n\n"
        "**[P1] ВАЛИДАЦИЯ T=0 СТАТИКИ:**\n"
        "Алгоритм:\n"
        "1. Найди процессы → удали (about to/begins/will/going to/approaching/moving toward)\n"
        "2. Найди future tense → замени на present state\n"
        "3. Найди последовательности (then/after) → оставь только T=0 состояние\n"
        "4. СОХРАНИ команды редактирования (create/generate/Edit image/reposition)\n\n"
        "**[P2] ГРАНИЦЫ КАДРА:**\n"
        "- shot_description > scene_action (приоритет описанию кадра)\n"
        "- Исключай элементы из previous_shot/next_shot\n"
        "- НЕ добавляй предметы, которых нет в shot_description на T=0\n"
        "- НЕ запрещай scene_characters в negative\n\n"
        "**[P1] КОНСИСТЕНТНОСТЬ КАМЕРЫ И ПЕРСОНАЖА:**\n"
        "- Если кадр подразумевает выступление/трибуну/микрофон/доклад и camera_position = in_front (камера в зале перед сценой):\n"
        "  • НЕ располагай ряды кресел/аудиторию как фон ЗА спиной выступающего (это ломает геометрию сцены).\n"
        "  • Если ряды кресел ДОЛЖНЫ быть видны в кадре при camera_position=in_front — они должны читаться как часть зала на стороне камеры (foreground/midground),\n"
        "    а сцена/экран/занавес/бэкстейдж остаются за персонажем.\n"
        "- Если camera_position = behind/over-shoulder (камера на сцене за спиной персонажа):\n"
        "  • тогда ряды кресел/аудитория логично видны ПЕРЕД персонажем (в глубине кадра),\n"
        "  • character_orientation обычно facing_away/profile (персонаж обращён к аудитории).\n"
        "- ВАЖНО (геометрия зала): если в кадре есть сцена/подиум/экран и кресла/аудитория, их взаимная ориентация должна быть физически согласованной:\n"
        "  кресла обращены к сцене, а подиум находится на стороне сцены, а не внутри зоны посадочных мест.\n"
        "  **Запрет 'audience on stage' (универсально):** ряды посадочных мест и основная аудитория НЕ могут находиться на сценическом помосте/подиуме,\n"
        "  если shot_description/storyboard не требует обратного. Если в кадре видны и сцена/подиум, и аудитория — явно укажи 'audience seated off-stage in the hall / seating area'\n"
        "  и добавь 1–2 маркера разделения: edge of raised stage platform visible, steps/risers, clear separation line between stage and first row.\n\n"
        "- **OUTFIT CONTINUITY:**\n"
        "  • Не меняй одежду/униформу персонажей без явного требования shot_description.\n"
        "  • Если `scene_continuity_facts.character_facts` содержит устойчивый гардероб (костюм/халат/униформа) — сохраняй его.\n"
        "  • Для CLOSE UP лица: добавь видимые подсказки одежды в пределах кадра (воротник/лацкан/край ткани), чтобы не было дрейфа в другую униформу.\n\n"
        "- **WARDROBE BY SCENE LOCATION:**\n"
        "  • Используй `location_time`/локацию сцены как жёсткий контекст гардероба (если shot_description не требует обратного).\n"
        "  • В CLOSE UP обязательно оставляй 1–2 видимых подсказки одежды, чтобы не было скачков костюм↔скрабы.\n\n"
        "- **AUDIENCE REACTION ZONE LOCK:**\n"
        "  • Если shot_description кадра про реакцию аудитории/учёного в зале (аплодирует/пишет/инсайт/реакция), субъект должен находиться в зоне seating area (in the audience seating / among the seated audience),\n"
        "    а не на сцене.\n"
        "  • Не добавляй фразы вроде \"на полу сцены\" / \"on stage floor\" для аудитории. Podium/lectern/microphone допускаются только как дальний фон/якорь сцены, если это не меняет смысл.\n\n"
        "- **MICROPHONE / PODIUM ANCHOR LOCK:**\n"
        "  • Если shot_description подразумевает выступление/речь/формулировку \"у микрофона\" ИЛИ про касание/контакт руки/когтя с микрофоном,\n"
        "    микрофон трактуется как podium microphone на стороне сцены (у трибуны), а не как ручной микрофон в зале.\n"
        "  • Если PRIMARY SUBJECT = рука/коготь: primary focus = рука/коготь + микрофон; не превращай такой кадр в \"лицо у микрофона\".\n"
        "  • Если PRIMARY SUBJECT = лицо у микрофона (CLOSE UP речи): лицо может быть в фокусе, но микрофон должен читаться как podium mic (не handheld),\n"
        "    и добавь короткую якорную фразу зоны: 'at the podium mic on stage (background blurred)'.\n"
        "  • Фон только blur-hint сценического света; НЕ делай читаемые ряды кресел/аудиторию как фон.\n"
        "  • Запрещено для конференц-зала: handheld microphone / mic in hand, если storyboard явно не требует handheld.\n\n"
        "**[P2] VIDEO_PROMPT:**\n"
        "- video_prompt должен быть ОДНОЙ строкой на английском, без кириллицы.\n"
        "- НЕ добавляй конкретные новые объекты/символы/холограммы/пропсы, которых нет в shot_description этого шота.\n"
        "- REACTION SHOT / SPLIT SCREEN: НЕ пиши 'looking at camera' если это не указано явно.\n"
        "- НЕ утверждай отсутствие объектов ('no X') если это не требуется shot_description.\n"
        "- **VIDEO_PROMPT = STORYBOARD ONLY (anti-drift):**\n"
        "  • video_prompt должен быть по смыслу ТОЛЬКО из storyboard.description (shot_description) этого шота.\n"
        "  • Запрещено придумывать одежду/гардероб (jeans/t-shirt/hoodie), читаемый текст/годы/логотипы (\"2024\", quotes), эффекты,\n"
        "    если этого нет в shot_description.\n"
        "  • Если входной video_prompt содержит такие элементы — УДАЛИ их и перепиши video_prompt как краткий перевод/парафраз shot_description.\n"
        "- **POV CHARACTER REFERENCE LOCK:**\n"
        "  • Если camera_plan/shot_description задаёт POV named персонажа (например \"POINT OF VIEW ([имя])\"):\n"
        "    - character reference этого персонажа ОБЯЗАТЕЛЕН даже если персонаж не виден в кадре.\n"
        "    - если в кадре видны части тела POV персонажа (руки/лапа/плечо/затылок) — они ДОЛЖНЫ соответствовать character reference (вид/анатомия/текстуры).\n"
        "- **OVER‑SHOULDER / BACK VIEW IDENTITY LOCK:**\n"
        "  • Если кадр подразумевает \"сзади по плечу/over-the-shoulder/from behind\" и в кадре видны части тела персонажа:\n"
        "    - видимые части тела ДОЛЖНЫ соответствовать character reference (силуэт/пропорции/текстуры).\n"
        "- **PROP HERO SHOT (object-in-flight)**: если shot_description фокусируется на одном движущемся объекте (\"летит\", \"flies\", \"arc\"):\n"
        "  • primary focus = этот объект; остальные персонажи/реквизит/анатомия пациента/инструменты/дисплеи — только как размытый фон И ТОЛЬКО если shot_description явно требует.\n"
        "  • НЕ переносить конкретные элементы операционного поля/пациента/мониторов из scene.action в кадр, если shot_description этого шота про полёт объекта.\n"
        "  • НЕ менять позиции персонажей: если они не являются первичным объектом shot_description — оставь их статичными/в фоне.\n"
        "  • **КАУЗАЛЬНЫЕ ЯКОРЯ**: чтобы объект не выглядел \"из ниоткуда\", в english_prompt разрешено добавить МИНИМАЛЬНЫЕ подсказки источника/цели,\n"
        "    если это не меняет PRIMARY SUBJECT и не добавляет новых предметов:\n"
        "    - source cue: \"from [character] chest pocket/hand\" или \"from off-screen left\" (выбери по смыслу контекста),\n"
        "    - trajectory + direction: \"clean parabolic arc across frame\",\n"
        "    - target cue (опционально): \"toward the operating table/surgical field\" как размытый ориентир.\n"
        "    Запрещено описывать \"открытый мозг/кровь/анатомию\" если этого нет в storyboard.description данного шота.\n"
        "  • **NO SUBSTITUTE OBJECTS**: не компенсируй отсутствие source/target выдуманными конкретными объектами (например \"3D brain model on monitor\") если их нет в storyboard.description.\n"
        "- **THROW / HURL / RELEASE LOCK:**\n"
        "  • Если `shot_description` содержит \"вышвыривает/выбрасывает/бросает/швыряет\" (throws/hurls/tosses):\n"
        "    - START (T=0): объект ЕЩЁ в лапе/руке (pre-release), но поза статична; НЕ делай объект уже в воздухе.\n"
        "    - END (T=final): объект УЖЕ НЕ в лапе/руке (post-release). По умолчанию объект уже ВНЕ КАДРА (если storyboard не требует видимого полёта в этом же шоте).\n"
        "  • COUNT LOCK: если по смыслу это один объект, запрещены дубликаты; вторая рука пустая.\n"
        "  • Negative: START → \"no duplicate [object]; other hand empty; no [object] in mid-air\"; END → \"no [object] in hand; no [object] visible\" (если полёт не требуется).\n"
        "- **FALLING PROP LANDING LOCK (falls into hand / lands in hand):**\n"
        "  • Если `shot_description` описывает \"падает в руку/опускается на руку\" (falls into his hand / lands in his hand):\n"
        "    - START (T=0): объект в воздухе над/рядом с ладонью, видимый зазор (no contact).\n"
        "    - END (T=final): объект уже в ладони/в руке (contact/received), НЕ зависает в воздухе.\n"
        "  • Запрещено держать объект \"висит в воздухе\" на ОБОИХ кадрах.\n"
        "- **CATCH / HANG BY THREAD LOCK (цепляется / висит на нитке):**\n"
        "  • Если `shot_description` содержит \"цепляется\", \"висит на одной нитке\", \"свисает\" / \"catches on the edge\", \"hangs by a single thread\":\n"
        "    - START (T=0): ранний момент падения — предмет только влетает/падает в кадр (в небе/над городом); НЕТ нитки, НЕТ точки крепления, НЕТ крупного края крыши.\n"
        "      (Если поверхность видна — она должна быть далеко и не как явный край зацепа.)\n"
        "    - END (T=final): пост‑зацеп — предмет ЗАЦЕПИЛСЯ за край и ВИСИТ на одной нитке; точка крепления должна читаться однозначно.\n"
        "    - Запрещено: “парит/hovering/levitating” вместо гравитации+зацепа.\n"
        "    - ПРИМЕРЫ: \"цепляется за край\" → START: предмет в воздухе, END: предмет зацепился за край; \"висит на нитке\" → START: предмет падает, END: предмет висит на нитке.\n"
        "- **FALLING PROP LANDING LOCK (onto body parts):**\n"
        "  • Если `shot_description` описывает \"падает на рог/голову/плечо/лапу/руку\" (falls onto his horn/head/shoulder/hand):\n"
        "    - START (T=0): предконтакт — предмет в воздухе, видимый зазор.\n"
        "    - END (T=final): постконтакт — предмет лежит/сидит на указанной части тела (результат), НЕ в воздухе.\n"
        "- **MATCH CUT RECIPIENT LOCK:**\n"
        "  • Если storyboard.description данного кадра — про получателя (например художник получает носок; человек получает бутерброд) и не упоминает бросающего в кадре,\n"
        "    не добавляй бросающего как видимого background персонажа: держи его off-screen.\n"
        "- **REFLECTION ≠ OBJECT LOCK (универсально):**\n"
        "  • Если `shot_description` описывает отражение (например \"отражение глаза\" в жирном блеске/металле/стекле), отражение должно оставаться отражением/бликом,\n"
        "    а НЕ превращаться в новый физический объект (\"eye embedded in sausage\", \"eyeball on food\", etc.).\n"
        "  • Для еды/реквизита: запрещена антропоморфизация (глаза/лица/рот/анатомия на еде), если storyboard.description прямо не требует такого хоррора.\n"
        "  • Если это уместно, добавь в negative_prompt: \"no eyes embedded in food/sausage\", \"no face on food\", \"no eyeball\".\n"
        "- **ENVIRONMENT HERO SHOT (зал/толпа/пауза)**: если shot_description кадра про зал/локацию/толпу/тишину:\n"
        "  • primary focus = пространство и массовая реакция (или её отсутствие).\n"
        "  • НЕ делай конкретного персонажа главным (и не добавляй трибуну/микрофон/сцену как якорь), если этого нет в shot_description.\n"
        "  • Персонажи допустимы только вторично (силуэты/мелкие фигуры для масштаба), без детальных поз/гардероба.\n"
        "ЕСЛИ video_prompt пустой ИЛИ содержит ТОЛЬКО 'locks off/static':\n"
        "→ Добавь минимальную микродинамику:\n"
        "- Close-up персонажа: 'Camera locks off; subject holds still with subtle breathing'\n"
        "- Close-up рук/объектов: 'Camera locks off; hands steady with minimal tremor'\n"
        "- Medium: 'Camera static; subject maintains position with slight adjustments'\n"
        "- Wide: 'Camera static; subjects positioned; atmospheric elements move gently'\n\n"
        "**[P2] NEGATIVE_PROMPT:**\n"
        "ПЕРЕД выводом проверь:\n"
        "- НЕ содержит имён из scene_characters → удали если есть\n"
        "- Правильно: 'no extra characters (excluding [scene_characters names])'\n"
        "- НЕ общие фразы ('no extra characters'), УКАЗЫВАЙ имена\n\n"
        "**[P1] ЗАПРЕЩЕНО:**\n"
        "- Процессы: about to / begins to / will / going to\n"
        "- Движение в T=0: walking / running / moving / approaching\n"
        "- Последовательности: then / after that / next\n\n"
        "**МИНИМАЛЬНОСТЬ**: Если JSON ОК → верни БЕЗ ИЗМЕНЕНИЙ. Правь ТОЛЬКО: english_prompt, negative_prompt, video_prompt.\n"
        "ФОРМАТ: СТРОГО JSON с теми же полями.\n"
        "- Приоритет: используй ТОЛЬКО описание ТЕКУЩЕГО шота (shot_description) как источник истинного состояния. scene_action и прочий контекст — фон, НО БЕЗ переноса будущих состояний. Исключение: сценоуровневые устойчивые состояния (грязный/мокрый/повреждения и т.п.), если явно заданы — см. правило ниже. Если есть конфликт — следуй shot_description.\n"
        "- СТРОГИЕ ГРАНИЦЫ КАДРА: исключай из english_prompt и video_prompt формулировки, относящиеся к ПРЕДЫДУЩЕМУ или СЛЕДУЮЩЕМУ кадру.\n"
        "  • Не описывай завершение действия из предыдущего кадра — фиксируй состояние на T=0 текущего кадра.\n"
        "  • Не переносй начало действия следующего кадра — убери любые 'then/after that/continues to' и оставь только уместную микродинамику текущего кадра.\n"
        "  • Используй краткий контекст previous_shot/next_shot только для фильтрации чужих действий, НЕ для добавления новых.\n"
        "- Запрет на ПРОЦЕССЫ В ДЕЙСТВИИ: убери из english_prompt формулировки ПРОЦЕССОВ ('about to', 'begins to', 'approaching'), НО СОХРАНЯЙ команды создания изображения ('create', 'generate', 'render', 'add', 'build', 'Increase image 1 by', 'Move subject away by'). НО НЕ добавляй 'Increase image 1 by 1.0x' — пропускай команду масштабирования при коэффициенте 1.0. Команды создания - это инструкции для генерации изображения с нуля, а не описания готовой сцены.\n"
        "- Не добавляй предметы/атрибуты, которых нет в shot_description на T=0 (напр. плащ/корона до коронации).\n"
        "- Не запрещай в negative персонажей, которые присутствуют.\n"
        "- Сохрани пространственные поля (camera_position, character_orientation, spatial_composition, point_of_view), если они не противоречат T=0.\n"
        "- Сценоуровневые устойчивые состояния: если в scene_action/контексте явно указаны устойчивые состояния персонажей или окружения (напр. \"грязное лицо\", \"мокрая одежда\"), И они НЕ противоречат shot_description — НЕ удаляй их и при необходимости ДОБАВЬ в english_prompt.\n"
        "- Дополнительно: `scene_continuity_facts` (если переданы во входном контексте) — это уже извлечённые устойчивые факты сцены (гардероб/прикреплённые предметы/фикс.элементы).\n"
        "  • Если camera_plan = Wide/Medium (или аналог в тексте) и персонаж присутствует — ДОБАВЬ отсутствующие устойчивые факты (например: 'тесный костюм', 'диплом на хвосте', 'булавка на лацкане'), если это не противоречит shot_description.\n"
        "  • Если camera_plan = Close-up/Extreme close-up — добавляй такие факты только если они реально попадают в кадр.\n"
        "  • **NAME/IDENTITY LOCK**: используй канонические имена персонажей ТОЛЬКО из `scene_characters`/`shot_description`.\n"
        "    Если во входном тексте есть искажённые/оскорбительные/ошибочные варианты имён — ОБЯЗАТЕЛЬНО нормализуй на каноническое имя.\n"
        "    НЕ цензурируй «токсичность» как класс; цель — корректность имён/сущностей и соответствие сториборду.\n"
        "- **ENTITY NATURE LOCK**: запрещено менять тип сущности персонажа.\n"
        "  • Если персонаж по `shot_description`/`scene_characters` = антропоморфное животное/животное/робот — НЕ превращай его в обычного человека.\n"
        "    Для широких/средних планов обязательно сохраняй читаемые нечеловеческие признаки (морда/чешуя/хвост; мех/морда/хвост; механическое тело).\n"
        "  • Если персонаж = человек — НЕ добавляй животные/роботические признаки.\n"
        "- Консистентность света/теней/стиля обеспечивается референсами. Не вставляй длинные санитайзеры в english_prompt.\n"
        "- Запрет drift: не допускай identity/style drift.\n"
        "- **ЦЕЛОСТНОСТЬ ЛОКАЦИИ**: сохраняй элементы из location_context.key_features как ЕДИНЫЕ объекты. Если там 'wall with embedded neon symbol' - НЕ разделяй на отдельные 'wall' и 'symbol'.\n"
        "- МАСШТАБИРОВАНИЕ И РЕФЕРЕНСЫ: если в english_prompt есть команды масштабирования ('Increase image 1 by', 'Move subject away by') — ОБЯЗАТЕЛЬНО ограничь reference_image_paths до ПЕРВОГО изображения! НЕ ДОБАВЛЯЙ 'Increase image 1 by 1.0x'.\n"
        "- ПОРЯДОК ИЗОБРАЖЕНИЙ: если референсов больше одного и масштабирования нет — СОХРАНИ исходный порядок и явное соответствие ролей этому порядку.\n"
        "- ПЕРЕФРАЗИРУЙ НЕГАТИВ В ПОЗИТИВ: заменяй 'do not distort face' на 'preserve facial identity and features'.\n"
        "- ПЕРСПЕКТИВА/ОККЛЮЗИИ: добавь при необходимости 'respect perspective and scale; ensure natural occlusion; avoid floating elements'.\n"
        "- СТИЛИЗАЦИЯ И СТИЛЬ РЕФЕРЕНСОВ: 'style-only — preserve geometry/composition; no repositioning' (нельзя менять геометрию/композицию/камеру). ОБЯЗАТЕЛЬНО используй visual_style из screenplay данных. ЯВНО требуй: 'preserve reference style and visual treatment; avoid style drift; match style and color grading across elements'. Удали фразы вроде 'occupying X% frame height'.\n"
        "- СТРУКТУРНАЯ ПРОВЕРКА: убедись, что english_prompt содержит все 6 компонентов: Действие + Объект + Позиция + Стиль + Освещение + Перспектива. Если отсутствует компонент — добавь.\n"
        "- КОНКРЕТНОСТЬ: замени размытые формулировки ('красиво', 'лучше', 'хорошо') на точные параметры (температура света в K, конкретные углы, процентные соотношения).\n"
        "- SCALING-ONLY МИНИМИЗАЦИЯ: если english_prompt содержит только простое зум/отъезд ('Increase image 1 by', 'Move subject away by') БЕЗ дополнительных изменений (камера, освещение, позиция) — упростить english_prompt до одной команды масштабирования + 'use image 1 as continuity reference'. ОБЯЗАТЕЛЬНО ограничь reference_image_paths до ПЕРВОГО изображения только. **ИСКЛЮЧЕНИЕ**: НЕ генерируй 'Increase image 1 by 1.0x' — при коэффициенте 1.0 пропускай команду масштабирования. **ВАЖНО**: если есть изменения камеры (camera_position, final_camera_yaw_deg ≠ 0, lighting_style комплексный) — СОХРАНЯЙ полное описание, не применяй минимизацию.\n"
        "- **ВАЛИДАЦИЯ МАСШТАБИРОВАНИЯ**: ОБЯЗАТЕЛЬНО проверь english_prompt на наличие бессмысленных команд масштабирования с коэффициентом 1.0 (например: 'Increase image 1 by 1.0x', 'Move subject away by 1.0x', 'Increase image 1 by 1x'). Если найдены — УДАЛИ их из команды, оставив только осмысленное редактирование (crop, reposition, lighting, style adjustments).\n"
        "- КРУПНЫЕ ПЛАНЫ ЧАСТЕЙ ТЕЛА: при фокусе на частях тела (руки, ноги, лицо и т.д.) — заменяй 'preserve facial identity' на 'preserve [body part] identity and anatomical details (scars, calluses, skin texture)'; усили негатив анатомии: 'no extra/double/missing [body parts], no deformed anatomy, no distorted proportions, no floating elements, no unnatural joints'.\n"
        "- КАЧЕСТВО И ДЕТАЛИЗАЦИЯ: добавь команды улучшения качества если отсутствуют: 'with enhanced clarity', 'preserve details', 'remove artifacts'.\n"
        "- КРУПНЫЕ ПЛАНЫ ЧАСТЕЙ ТЕЛА: если spatial_composition описывает крупный план рук/ног с частичным присутствием лица/тела — НЕ удаляй упоминания лица/тела из english_prompt, это необходимо для сохранения идентичности персонажа.\n"
        "- **ЭКРАНЫ/ИНТЕРФЕЙСЫ**: при планшете/экране используй 'no brand logos, no long text' вместо 'no text' (разреши UI элементы)\n"
        "- **УНИФИКАЦИЯ УСТРОЙСТВ**: всегда 'tablet' для деловых сцен, 'smartphone' только для звонков\n"
        "- **НОРМАЛИЗАЦИЯ ТИПОВ**: булевы поля как true/false (не строки), character_orientation из enum, point_of_view как objective/subjective/pov_[имя]\n"
        "- Минимальность правок: если входной JSON УЖЕ соответствует этим правилам — верни его БЕЗ ИЗМЕНЕНИЙ. Вноси ТОЛЬКО необходимые точечные правки (преимущественно в english_prompt и negative_prompt). Не меняй characters/location/пространственные поля без явного противоречия.\n"
        "Ответ возвращай СТРОГО как JSON с теми же полями."
    )

    context_summary = {
        "shot_description": extended_context.get("shot_description", ""),
        "scene_action": extended_context.get("scene_action", ""),
        "scene_continuity_facts": extended_context.get("scene_continuity_facts", {}),
        "scene_characters": extended_context.get("scene_characters", []),
        "previous_shot": extended_context.get("previous_shot", {}),
        "next_shot": extended_context.get("next_shot", {}),
        "current_shot_position": extended_context.get("current_shot_position", ""),
    }

    user_prompt = (
        f"Санитайз START кадра (T=0). Минимум правок. Если ОК → верни без изменений.\n\n"
        f"<context>\n"
        f"shot_description (ПРИОРИТЕТ): {json.dumps(context_summary['shot_description'], ensure_ascii=False)}\n"
        f"scene_continuity_facts: {json.dumps(context_summary['scene_continuity_facts'], ensure_ascii=False)}\n"
        f"scene_characters: {json.dumps(context_summary['scene_characters'], ensure_ascii=False)}\n"
        f"</context>\n\n"
        f"<raw_start_result>\n{json.dumps(start_result, ensure_ascii=False, indent=2)}\n</raw_start_result>\n\n"
        "**ЗАДАЧА:**\n"
        "1. **МАСШТАБ**: Удали 'by 1.0x/1x/0.98x/0.99x/1.01x/1.02x'\n"
        "2. **T=0 СТАТИКА**: Удали about to/begins/will/walking/running/moving/approaching в english_prompt; не удаляй 'then' внутри video_prompt (там допустимо до 2 шагов)\n"
        "3. **VIDEO_PROMPT**: Если пустой → добавь микродинамику (breathing/tremor/mist drifts)\n"
        "4. **NEGATIVE**: Проверь НЕТ scene_characters имён → удали если есть\n\n"
        "Верни JSON."
    )

    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=8000,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        clean_resp = extract_json_from_markdown(response)
        return json.loads(clean_resp)
    except Exception as e:
        logger.error(f"❌ Ошибка LLM-санитайза START: {e}")
        return None


def _sanitize_end_via_llm(
    start_result: Dict[str, Any],
    end_result: Dict[str, Any],
    video_prompt: str,
    extended_context: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    LLM-санитайзер для END: выравнивает с START и video_prompt, запрещает новые персонажи/локации без основания,
    проверяет should_link_as_next_start по строгим критериям, при необходимости корректирует.
    Возвращает отредактированный JSON end_result.
    """
    system_prompt = (
        "Ты — санитайзер континуити. Приведи END-кадр к преемственности с START и video_prompt.\n\n"
        "**ПРИОРИТЕТЫ ПРАВОК:**\n"
        "1. [P1] Масштабирование 1.0±0.02 → удалить\n"
        "2. [P1] Prop_continuity → синхронизация с промптами\n"
        "3. [P1] CONTINUITY LOCK (фон/props) → запрет новых объектов без основания\n"
        "4. [P1] MOVING PROP DELTA → если в кадре есть летящий/падающий предмет, END обязан фиксировать другую позицию\n"
        "3. [P2] Линковка → валидация по алгоритму\n"
        "4. [P3] Минимум остальных правок\n\n"
        "**[P1] ВАЛИДАЦИЯ МАСШТАБИРОВАНИЯ:**\n"
        "Удали команды с ratio ≈ 1.0:\n"
        "❌ 'Increase by 1.0x' / 'by 1x' / 'by 0.98x' / 'by 0.99x' / 'by 1.01x' / 'by 1.02x'\n"
        "✅ Сохрани: 'by 1.2x', 'by 0.8x'\n\n"
        "**[P1] ГРАНИЦЫ КАДРА:**\n"
        "- T=final = результат ПОСЛЕ действий (НЕ процесс)\n"
        "- Исключай then/after/continues to (следующий кадр)\n"
        "- Запрещено: removing/approaching/moving toward/drifting\n\n"
        "**[P1 CRITICAL] NO-INVENTION ДЛЯ END (особенно для CLOSE UP / EXTREME CLOSE UP):**\n"
        "- Источник истины для объектов/действий в кадре: ТОЛЬКО shot_description (storyboard.description) текущего кадра.\n"
        "- НЕЛЬЗЯ подтягивать конкретные объекты/интерфейсные элементы/считываемые источники информации из scene.action или location_context\n"
        "  если они не упомянуты в shot_description данного шота.\n"
        "- Для Close-up/Extreme close-up: фон должен оставаться общим и размытым, без перечисления конкретных предметов.\n"
        "  Если shot_description про руку/когти/скальпель/контакт/лицо реакции — не добавляй в кадр конкретные источники информации как видимые объекты.\n\n"
        "**[P1 CRITICAL] SCENE VS SHOT CONTEXT:**\n"
        "- `scene.action` даёт общий контекст сцены, НО НЕ является источником конкретных объектов для данного шота.\n"
        "- Если модель “перетаскивает” объекты/якоря из `scene.action` (мониторы, экраны, мозг на дисплее, etc.) в END,\n"
        "  когда `shot_description` этого шота про другое (например рука+скальпель, лица реакции, полёт бутерброда),\n"
        "  это СЧИТАЕТСЯ ОШИБКОЙ: удали такие детали и оставь фон обобщённым/размытым.\n\n"
        "**[P1 CRITICAL] PROP HERO SHOT (object-in-flight) — END:**\n"
        "- Если `shot_description` этого шота описывает полёт/дугу/slow-motion движущегося объекта, то END-кадр:\n"
        "  • обязан оставлять primary focus на летящем объекте;\n"
        "  • НЕ должен материализовать конкретную анатомию пациента/операционное поле/инструменты/дисплеи как видимые объекты,\n"
        "    если это не требуется именно `shot_description` данного шота;\n"
        "  • остальные персонажи, если они не первичный субъект `shot_description`, должны оставаться вторичными/статичными/в размытом фоне.\n\n"
        "**[P2 REQUIRED] MICROPHONE / PODIUM ANCHOR LOCK — END:**\n"
        "- Если `shot_description` этого шота подразумевает выступление/речь/формулировку \"у микрофона\" ИЛИ про контакт/касание руки/когтя с микрофоном,\n"
        "  микрофон должен пониматься как podium microphone на стороне сцены (у трибуны), а не как ручной микрофон в зале.\n"
        "- Если PRIMARY SUBJECT = рука/коготь: primary focus = рука/коготь + микрофон (контакт как стоп‑кадр результата). Не переводить кадр в \"лицо у микрофона\".\n"
        "- Если PRIMARY SUBJECT = лицо у микрофона: лицо может быть в фокусе, но микрофон должен читаться как podium mic (не handheld);\n"
        "  добавь короткую якорную фразу зоны: 'at the podium mic on stage (background blurred)'.\n"
        "- Фон: только общий blur-hint сценического света; не делай читаемые ряды кресел/аудиторию как фон.\n"
        "- Запрещено для конференц-зала: handheld microphone / mic in hand, если storyboard явно не требует handheld.\n\n"
        "**[P1] PROP_CONTINUITY ВАЛИДАЦИЯ:**\n"
        "\n"
        "FOR EACH item IN prop_continuity:\n"
        "  IF status = \"removed\":\n"
        "    → english: \"with [item] removed\"\n"
        "    → negative: \"no [item] in [CHARACTER] hands\" (НЕ общее \"no [item]\")\n"
        "  ELSE IF status = \"added\":\n"
        "    → english: \"add [item] to [position]\"\n"
        "  ELSE IF status = \"kept\" AND has_state (\"partially\", \"being\", \"hanging\"):\n"
        "    → english: ЯВНО опиши состояние (\"with [item] [state]\")\n\n"
        "**ПРИМЕРЫ:**\n"
        "✅ removed: english: \"with right glove removed\", negative: \"no right glove in Father's hands\"\n"
        "✅ kept + state: \"with left glove partially removed and hanging from fingers\"\n"
        "✅ added: \"add hammer to right hand\"\n\n"
        "**[P2] ВАЛИДАЦИЯ ДЕЛЬТЫ:**\n"
        "framing_delta_percent и subject_scale_ratio УЖЕ рассчитаны.\n"
        "ЗАДАЧА: ВАЛИДИРУЙ соответствие с video_prompt. Расхождение >10% → ИСПРАВЬ.\n\n"
        "**[P2] ЛИНКОВКА ВАЛИДАЦИЯ:**\n"
        "ВАЛИДИРУЙ:\n"
        "- IF both_false → english_prompt НЕ содержит 'continue to next shot'\n"
        "- IF should_link_as_next_start = true → continuity_score >= 8\n"
        "- Расхождение → ИСПРАВЬ на reference (консервативно)\n\n"
        "**[P2] ФИНАЛЬНЫЕ ПАРАМЕТРЫ:**\n"
        "Используй ВСЕ: final_spatial_composition, final_camera_angle, final_lighting_style, final_character_orientation\n\n"
        "**[P2] NEGATIVE_PROMPT:**\n"
        "- НЕ общие фразы ('no gloves') → УКАЗЫВАЙ персонажа ('no gloves in Father's hands')\n"
        "- Если shot_description = crowd/group → НЕ используй 'no duplicates'\n\n"
        "**[P1] ЗАПРЕЩЕНО:**\n"
        "- Процессы: removing / approaching / moving toward / drifting\n"
        "- Последовательности: then / after that / continues to\n"
        "- Масштаб ≈1.0: 1.0x / 1x / 0.99x / 1.01x\n\n"

        "**[P1] CONTINUITY LOCK (ФОН/PROPS/МЕБЕЛЬ/ДЕКОР):**\n"
        "- Если END это редактирование на базе START (Edit image 1 / image 1 как continuity reference), то:\n"
        "  • НЕ добавляй новые предметы интерьера/мебель/подносы/столики/тележки/доп.оборудование, которых нет в START,\n"
        "    если они явно не упомянуты в storyboard.description или scene_action.\n"
        "  • Если в end_result.english_prompt присутствуют такие добавления (например: \"инструменты на подносе\", \"столик\", \"тележка\") — УДАЛИ их из english_prompt.\n"
        "  • Если end_result.negative_prompt не запрещает новые объекты — при необходимости добавь запреты типа: \"no new trays\", \"no new tables\", \"no new trolleys\" (на языке negative_prompt).\n"
        "- ВАЖНО: location reference используется только для палитры/материалов/геометрии, НЕ для добавления новых объектов.\n\n"

        "**[P1] MOVING PROP DELTA (ЛЕТЯЩИЙ РЕКВИЗИТ):**\n"
        "- Если storyboard.description ИЛИ video_prompt содержит признаки движения объекта (например: \"летит\", \"падает\", \"flies\", \"falls\", \"drops\"):\n"
        "  • END english_prompt ОБЯЗАН явно фиксировать финальную позицию этого объекта, отличающуюся от START.\n"
        "  • Опиши позицию НЕ абстрактно, а через 2 якоря: (a) зона кадра (верхняя/средняя/нижняя треть, левее/правее центра),\n"
        "    (b) расстояние до ключевого объекта/поверхности (например: \"2–3 см над\", \"в 10–15 см от\", \"почти касается, но зазор виден\").\n"
        "  • Если END и START описывают одну и ту же позицию (по смыслу) — переформулируй END так, чтобы позиция была заметно иной.\n"
        "  • НЕЛЬЗЯ компенсировать отсутствие дельты только масштабированием; нужна именно смена положения.\n\n"
        "**[P1] CATCH / HANG BY THREAD (цепляется / висит на нитке):**\n"
        "- Если shot_description содержит \"цепляется/висит на одной нитке/свисает\" или EN \"catches on the edge/hangs by a single thread\":\n"
        "  • END обязан фиксировать ПОСТ‑ЗАЦЕП: есть край крыши/кромка + один натянутый участок нитки + носок висит (не парит).\n"
        "  • Удали/почини любые формулировки в END, которые оставляют предмет \"в воздухе\" без крепления.\n"
        "  • Если end_result.negative_prompt запрещает зацеп (например 'no thread/attachment') — УДАЛИ этот запрет.\n\n"

        "**[P1] КОНСИСТЕНТНОСТЬ КАМЕРЫ И ПЕРСОНАЖА:**\n"
        "- Если кадр подразумевает выступление/трибуну/микрофон/доклад/обращение к залу:\n"
        "  • При camera_position = in_front (камера в зале перед сценой) — аудитория находится ЗА камерой и НЕ должна быть нарисована как фон за спиной персонажа.\n"
        "    УДАЛИ из english_prompt любые формулировки типа 'ряды кресел/зрителей позади', если они противоречат этому.\n"
        "    Фон за персонажем в таком случае: экран/голограмма/занавес/сцена.\n"
        "  • Если в кадре явно должны быть видны ряды кресел/аудитория — camera_position должен быть behind/over-shoulder,\n"
        "    а character_orientation должен быть facing_away/profile так, чтобы персонаж был обращён к аудитории.\n"
        "- Это правило важнее декоративных деталей и должно применяться даже если остальной JSON формально корректен.\n\n"

        "**ПРИОРИТЕТ ПРАВОК P1-P2**: Применяй ОБЯЗАТЕЛЬНО, даже если JSON корректен.\n"
        "**МИНИМАЛЬНОСТЬ P3**: Только для низкоприоритетных правок.\n\n"
        "Правь: english_prompt, negative_prompt, framing_delta_percent, subject_scale_ratio, linking flags.\n"
        "ФОРМАТ: JSON с теми же полями end_result.\n"
        "- СТРОГИЕ ГРАНИЦЫ КАДРА: english_prompt для END должен описывать ТОЛЬКО финальное состояние ТЕКУЩЕГО кадра.\n"
        "  • Исключай любые фразы, относящиеся к ПРЕДЫДУЩЕМУ или СЛЕДУЮЩЕМУ кадру ('then/after/continues to/meanwhile').\n"
        "  • Если часть описания относится к соседним кадрам (по previous_shot/next_shot), удали её, сохранив только итог T=END текущего кадра.\n"
        "  • Не переносй действия следующего кадра и не описывай завершение предыдущего — фиксируй результат, достигнутый в рамках текущего video_prompt.\n"
        "- Персонажи (важно):\n"
        "  • **PRIMARY SUBJECT LOCK (по ТЕКУЩЕМУ кадру)**: главный субъект END-кадра определяется `shot_description` (storyboard.description) ЭТОГО кадра.\n"
        "    - Он МОЖЕТ отличаться от START, если камера в video_prompt переходит фокус/POV на другого субъекта, и это отражено в shot_description.\n"
        "    - Запрещено менять главного субъекта на “более яркого” персонажа/объект, если shot_description этого не требует.\n"
        "  • shot_description — источник истины для ИМЕННОВАННЫХ участников кадра (не добавляй новых named персонажей, если они не упомянуты в shot_description).\n"
        "  • video_prompt НЕ является источником списка персонажей (он может не перечислять участников), НО является источником динамики кадра.\n"
        "  • Если по video_prompt / ratio / framing_delta_percent видно, что кадр РАСШИРЯЕТСЯ (zoom out / pull back / widening; ratio < 1.0 или framing_delta_percent << 0),\n"
        "    то в END может стать видна аудитория/массовка/другие люди КАК ФОН (без персональных имён), даже если они не перечислены в shot_description.\n"
        "    В таком случае добавляй их в english_prompt только как обобщённые background extras (\"audience\", \"other scientists\", \"staff in the background\") и НЕ позволяй им стать главным субъектом,\n"
        "    **ЕСЛИ только shot_description не говорит, что именно они становятся главным субъектом в END.**\n"
        "  • Если кадр СУЖАЕТСЯ (zoom in / push in; ratio > 1.0), допускается что второстепенные персонажи/массовка выходят из кадра — убери их из english_prompt.\n"
        "- Локация: не менять, если video_prompt не указывает смену.\n"
        "- Камера/фрейминг: если в video_prompt нет движения — не менять фрейминг.\n"
        "- ЧИСЛОВАЯ ДЕЛЬТА ФРЕЙМИНГА обязательна: проверь и при необходимости добавь/исправь поля framing_delta_percent (−60…+60) и subject_scale_ratio (>0), используя таблицу трансляции;\n"
        "  ЕСЛИ присутствуют одновременно движение камеры и персонажа вдоль оптической оси — рассчитай СУММАРНУЮ дельту (учти компенсации/усиления).\n"
        "- Реквизит и гардероб/состояния (КРИТИЧНО): запрети самопоявление/пропажу предметов и несогласованные изменения одежды/аксессуаров/состояний (намок, грязь, повреждения). ОБЯЗАТЕЛЬНО учти КАЖДЫЙ элемент из `prop_continuity`: removed items должны отсутствовать в описании, added items должны присутствовать, а kept items с состояниями ('partially removed', 'hanging from', 'being taken off') должны быть ЯВНО описаны в english_prompt. Сценоуровневые устойчивые состояния (напр. \"грязное лицо\") — сохраняй, если video_prompt не указывает обратного.\n"
        "- Дополнительно: `scene_continuity_facts` (если переданы во входном контексте) — устойчивые факты сцены.\n"
        "  • Если camera_plan = Wide/Medium и персонаж в кадре — ДОБАВЬ отсутствующие устойчивые факты (гардероб/прикреплённые предметы), если это не противоречит shot_description.\n"
        "  • Если camera_plan = Close-up/Extreme close-up — добавляй такие факты только если они реально попадают в кадр.\n"
        "  • **NAME/IDENTITY LOCK**: используй канонические имена персонажей ТОЛЬКО из `scene_characters`/`shot_description`.\n"
        "    Если во входном тексте есть искажённые/оскорбительные/ошибочные варианты имён — ОБЯЗАТЕЛЬНО нормализуй на каноническое имя.\n"
        "    НЕ цензурируй «токсичность» как класс; цель — корректность имён/сущностей и соответствие сториборду.\n"
        "- Фокус только на финальном стоп-кадре (результат, а не процесс).\n"
        "- СОХРАНЯЙ команды редактирования в english_prompt ('Increase image 1 by', 'Move subject away by', 'reposition', 'reduce', 'maintain', 'apply'). НО НЕ добавляй 'Increase image 1 by 1.0x' — пропускай команду масштабирования при коэффициенте 1.0. Это НЕ процессы, а инструкции для генерации финального изображения.\n"
        "- Обязательно отрази ВСЕ параметры из final_spatial_composition, final_camera_angle, final_lighting_style, final_camera_position, final_character_orientation, final_point_of_view простыми командами: позиция (left/center/right), глубина (foreground/midground/background), доминирование фона (reduce/maintain), масштаб коэффициентами (Increase image 1 by / Move subject away by — БЕЗ упоминания процентов кадра), ракурс камеры, освещение, ориентацию персонажа, точку зрения.\n"
        "- Консистентность света/теней/стиля обеспечивается референсами. Не вставляй длинные санитайзеры в english_prompt.\n"
        "- ОТРАЖАЙ УГЛЫ ПОВОРОТА КАМЕРЫ: если final_camera_yaw_deg/ final_camera_pitch_deg ≠ 0 — отрази в команде, НЕ меняя масштаб.\n"
        "- ФОКУС/ГЛУБИНА РЕЗКОСТИ: для rack focus/focus pulls — выставь final_focus_target и final_depth_of_field и отрази в english_prompt.\n"
        "- MAIN SUBJECT/DEPTH ORDER: если заданы — отрази первенство главного субъекта и относительную глубину слоев.\n"
        "- Identity/style drift: запрещено. Сохраняй идентичность персонажей и общий стиль сцены.\n"
        "- **ЦЕЛОСТНОСТЬ ЛОКАЦИИ**: сохраняй элементы из location_context.key_features как ЕДИНЫЕ объекты. Если там 'wall with embedded neon symbol' - НЕ разделяй на отдельные 'wall' и 'symbol'.\n"
        "- МАСШТАБИРОВАНИЕ И РЕФЕРЕНСЫ: если в english_prompt есть команды масштабирования ('Increase image 1 by', 'Move subject away by') — ОБЯЗАТЕЛЬНО ограничь reference_image_paths до ПЕРВОГО изображения!'.\n"
        "- ПОРЯДОК ИЗОБРАЖЕНИЙ: если референсов больше одного и масштабирования нет — СОХРАНИ исходный порядок и явное соответствие ролей этому порядку.\n"
        "- ПЕРЕФРАЗИРУЙ НЕГАТИВ В ПОЗИТИВ: заменяй 'do not distort face' на 'preserve facial identity and features'.\n"
        "- ПЕРСПЕКТИВА/ОККЛЮЗИИ: добавь при необходимости 'respect perspective and scale; ensure natural occlusion; avoid floating elements'.\n"
        "- СТИЛИЗАЦИЯ И СТИЛЬ РЕФЕРЕНСОВ: 'style-only — preserve geometry/composition; no repositioning' (нельзя менять геометрию/композицию/камеру). ОБЯЗАТЕЛЬНО используй visual_style из screenplay данных. ЯВНО требуй: 'preserve reference style and visual treatment; avoid style drift; match style and color grading across elements'. Удали фразы вроде 'occupying X% frame height'.\n"
        "- СТРУКТУРНАЯ ПРОВЕРКА: убедись, что english_prompt содержит все 6 компонентов: Действие + Объект + Позиция + Стиль + Освещение + Перспектива, а также при наличии — углы камеры и фокус/DoF. Если отсутствует компонент — добавь.\n"
        "- КОНКРЕТНОСТЬ: замени размытые формулировки ('красиво', 'лучше', 'хорошо') на точные параметры (температура света в K, конкретные углы, процентные соотношения).\n"
        "- КАЧЕСТВО И ДЕТАЛИЗАЦИЯ: добавь команды улучшения качества если отсутствуют: 'enhance quality', 'preserve details', 'remove artifacts'.\n"
        "- ВРЕМЕННЫЕ/СЕЗОННЫЕ ИЗМЕНЕНИЯ: если video_prompt указывает изменения времени/сезона — добавь конкретные команды ('change time to evening', 'add winter snow effects').\n"
        "- «Смотрит на объект»: если в START/video_prompt герой смотрел на объект (фото и т.д.), убедись, что финальные параметры (final_camera_position, final_character_orientation) НЕ фронтальные. Исправь `english_prompt` и `negative_prompt`, чтобы объект был повернут к персонажу, а не к камере.\n"
        "- Пересчитай should_link_as_next_start по критериям:\n"
        "  composition_stability=='stable' И camera_movement_completed==true И continuity_score>=8 → true, иначе false.\n"
        "- Если строгая линковка false, но continuity_score>=5 ИЛИ композиция transitional → should_use_prev_end_as_reference=true, иначе false.\n"
        "- Минимальность правок: если входной JSON УЖЕ соответствует правилам — верни его БЕЗ ИЗМЕНЕНЕНИЙ. Правь точечно (english/negative/флаги/фрейминг/prop_continuity). Поля персонажей/локации/пространства не меняй без явного противоречия START или video_prompt.\n"
        "- **ВАЛИДАЦИЯ МАСШТАБИРОВАНИЯ**: ОБЯЗАТЕЛЬНО проверь english_prompt на наличие бессмысленных команд масштабирования с коэффициентом 1.0 (например: 'Increase image 1 by 1.0x', 'Move subject away by 1.0x', 'Increase image 1 by 1x'). Если найдены — УДАЛИ их из команды, оставив только осмысленное редактирование (crop, reposition, lighting, style adjustments).\n"
        "- КРУПНЫЕ ПЛАНЫ ЧАСТЕЙ ТЕЛА: при фокусе на частях тела (руки, ноги, лицо и т.д.) — заменяй 'preserve facial identity' на 'preserve [body part] identity and anatomical details (scars, calluses, skin texture)'; усили негатив анатомии: 'no extra/double/missing [body parts], no deformed anatomy, no distorted proportions, no floating elements, no unnatural joints'.\n"
        "- СИНХРОНИЗАЦИЯ С PROP_CONTINUITY: синхронизируй negative_prompt с prop_continuity: если предмет удален (removed) — добавь 'no [item] on [location]'; если предмет сохранен (kept) с промежуточным состоянием ('partially removed', 'hanging', 'being taken off') — убери запреты на предмет И добавь явное описание состояния в english_prompt; если добавлен (added) — разреши в negative.\n"
        "- **ЭКРАНЫ/ИНТЕРФЕЙСЫ/БАННЕРЫ/ЛЮБЫЕ ВИЗУАЛЬНЫЕ ЭЛЕМЕНТЫ**: разреши UI элементы\n"
        "- **НОРМАЛИЗАЦИЯ ТИПОВ**: булевы поля как true/false (не строки), character_orientation из enum, point_of_view как objective/subjective/pov_[имя]\n"
        "Ответ верни СТРОГО в формате JSON, сохраняя структуру полей end_result."
    )

    context = {
        "start_result": start_result,
        "end_result": end_result,
        "video_prompt": video_prompt,
        "scene_continuity_facts": extended_context.get("scene_continuity_facts", {}),
        "previous_shot": extended_context.get("previous_shot", {}),
        "next_shot": extended_context.get("next_shot", {}),
        "current_shot_position": extended_context.get("current_shot_position", ""),
    }

    user_prompt = (
        f"Санитайз END кадра (T=final). Приоритет P1-P2. Если ОК → верни без изменений.\n\n"
        f"<context>\n"
        f"video_prompt: {json.dumps(context['video_prompt'], ensure_ascii=False)}\n"
        f"scene_continuity_facts: {json.dumps(context['scene_continuity_facts'], ensure_ascii=False)}\n"
        f"next_shot: {json.dumps(context['next_shot'], ensure_ascii=False)}\n"
        f"</context>\n\n"
        f"<raw_end_result>\n{json.dumps(context['end_result'], ensure_ascii=False, indent=2)}\n</raw_end_result>\n\n"
        "**ЗАДАЧА:**\n"
        "1. **МАСШТАБ**: Удали 'by 1.0x/1x/0.98x/0.99x/1.01x/1.02x'\n"
        "2. **PROP_CONTINUITY**: Проверь removed → negative 'no [item] in [CHARACTER] hands'\n"
        "3. **T=final**: Удали removing/approaching/drifting/then/after\n"
        "4. **ЛИНКОВКА**: Валидируй should_link (continuity_score >= 8)\n"
        "5. **DELTA**: Валидируй соответствие delta/ratio с video_prompt\n\n"
        "Верни JSON."
    )

    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=8000,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        clean_resp = extract_json_from_markdown(response)
        result = json.loads(clean_resp)
        
        # Валидируем negative_prompt против prop_continuity
        if "negative_prompt" in result and "prop_continuity" in result:
            result["negative_prompt"] = _validate_negative_prompt_consistency(
                result["negative_prompt"], 
                result["prop_continuity"]
            )
        
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка LLM-санитайза END: {e}")
        return None
