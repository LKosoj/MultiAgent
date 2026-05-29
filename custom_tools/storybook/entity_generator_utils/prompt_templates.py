from typing import Dict, Any, List, Optional


def get_character_nature(entity_data: Dict[str, Any]) -> str:
    """
    Универсальная классификация типа персонажа без хардкода под конкретные виды.

    Предпочтение:
    1) Явное поле entity_nature (если есть): human | anthropomorphic_animal | animal | robot | other
    2) Эвристика по текстам (role/immutable/unique_features/...) на случай старых проектов.
    """
    if not isinstance(entity_data, dict):
        return "other"

    explicit = (entity_data.get("entity_nature") or entity_data.get("character_nature") or "").strip().lower()
    if explicit in {"human", "anthropomorphic_animal", "animal", "robot", "other"}:
        return explicit

    def _iter_texts(obj: Any):
        if obj is None:
            return
        if isinstance(obj, str):
            yield obj
            return
        if isinstance(obj, dict):
            for v in obj.values():
                yield from _iter_texts(v)
            return
        if isinstance(obj, list):
            for v in obj:
                yield from _iter_texts(v)
            return

    blob = " ".join(t.lower() for t in _iter_texts(entity_data))
    # robot / android
    if any(k in blob for k in ["робот", "андроид", "киборг", "android", "robot", "cyborg", "mechanical", "metal body", "screen face"]):
        return "robot"
    # anthropomorphic animal (фурри/антро)
    if any(k in blob for k in ["антропоморф", "фурри", "furry", "anthropomorphic"]):
        return "anthropomorphic_animal"
    # explicit human
    if ("человек" in blob) or (" human" in blob) or blob.startswith("human"):
        return "human"
    # generic animal signals
    if any(k in blob for k in ["животн", "animal", "muzzle", "snout", "fur", "tail", "paws"]):
        return "animal"
    return "other"


def _nature_constraints_text(entity_name: str, nature: str) -> str:
    """
    Строгие (но универсальные) ограничения для канон-референса, чтобы не было дрейфа типа сущности.
    """
    if nature == "human":
        return (
            "HUMAN-ONLY CONSTRAINT: This character is a HUMAN person. "
            "No animal traits (no fur, muzzle/snout, animal ears, tail, paws). "
            "No robot/cyborg parts (no metal body, screen face, mechanical limbs). "
        )
    if nature == "anthropomorphic_animal":
        return (
            "ANTHROPOMORPHIC-ANIMAL CONSTRAINT: This character is an ANTHROPOMORPHIC ANIMAL (furry). "
            "Must have clear animal traits (fur + muzzle/snout + animal ears and/or tail) while keeping humanoid posture. "
            "Do NOT render as a normal human; do NOT render as a robot/cyborg. "
        )
    if nature == "animal":
        return (
            "ANIMAL-ONLY CONSTRAINT: This character is a NON-HUMAN ANIMAL. "
            "Quadruped or anatomically animal-like; do NOT humanize into biped/furry humanoid; do NOT render as robot/cyborg. "
        )
    if nature == "robot":
        return (
            "ROBOT-ONLY CONSTRAINT: This character is a ROBOT/ANDROID. "
            "Clearly mechanical/metallic; no organic skin/fur; no animal traits; no human flesh anatomy. "
        )
    return ""

def build_canon_image_prompt(entity_type: str, entity_data: Dict[str, Any], consistency_rules: List[Dict[str, Any]]) -> str:
    """
    Создаёт детальный промпт для генерации канонического референса с акцентом на уникальность.
    Включает логику Location Sheet для локаций.
    """
    entity_name = entity_data.get("name", "Unknown")
    
    # Базовый промпт
    if entity_type == "character":
        base_prompt = f"Create canonical reference image of character '{entity_name}'. "
        base_prompt += "Show ONLY this ONE character against neutral background. "
        base_prompt += f"CRITICAL: Character '{entity_name}' must be VISUALLY DISTINCT and UNIQUE from other characters. "
        base_prompt += "Use reference images ONLY for style consistency (art style, color palette, lighting technique), NOT for character appearance. "
        nature = get_character_nature(entity_data)
        constraints = _nature_constraints_text(entity_name, nature)
        if constraints:
            base_prompt += constraints
        base_prompt += f"Character '{entity_name}' must have these SPECIFIC and UNIQUE features:\n"
        
        # Динамически добавляем все атрибуты персонажа (кроме name и reference_image_path)
        # Сначала immutable_attributes - самые важные для уникальности
        immutable = entity_data.get("immutable_attributes", {})
        if isinstance(immutable, dict):
            base_prompt += "\nIMMUTABLE (UNCHANGEABLE) FEATURES:\n"
            for sub_key, sub_value in immutable.items():
                if isinstance(sub_value, list) and sub_value:
                    items_text = ", ".join(str(item) for item in sub_value)
                    field_name = sub_key.replace('_', ' ').title()
                    base_prompt += f"- {field_name}: {items_text}\n"
                elif isinstance(sub_value, str) and sub_value.strip():
                    field_name = sub_key.replace('_', ' ').title()
                    base_prompt += f"- {field_name}: {sub_value}\n"
        
        # Затем variable_attributes
        variable = entity_data.get("variable_attributes", {})
        if isinstance(variable, dict):
            base_prompt += "\nCLOTHING AND APPEARANCE:\n"
            for sub_key, sub_value in variable.items():
                if isinstance(sub_value, list) and sub_value:
                    items_text = ", ".join(str(item) for item in sub_value)
                    field_name = sub_key.replace('_', ' ').title()
                    base_prompt += f"- {field_name}: {items_text}\n"
                elif isinstance(sub_value, str) and sub_value.strip():
                    field_name = sub_key.replace('_', ' ').title()
                    base_prompt += f"- {field_name}: {sub_value}\n"
        
        # Остальные атрибуты
        for key, value in entity_data.items():
            if key in ["name", "reference_image_path", "immutable_attributes", "variable_attributes"]:
                continue
                
            if isinstance(value, list) and value:
                items_text = ", ".join(str(item) for item in value)
                field_name = key.replace('_', ' ').title()
                base_prompt += f"\n{field_name}: {items_text}"
            elif isinstance(value, str) and value.strip():
                field_name = key.replace('_', ' ').title()
                base_prompt += f"\n{field_name}: {value}"
        
        # CHARACTER SHEET: multi-view для лучшего reference matching
        base_prompt += "\n\nFORMAT INSTRUCTION: Create a CHARACTER SHEET with multiple views of the SAME character:\n"
        base_prompt += "1. Main view: Front-facing 3/4 portrait (head to waist)\n"
        base_prompt += "2. Side view: Profile view showing silhouette and proportions\n"
        base_prompt += "3. Detail view: Face close-up showing eye color, skin texture, unique features\n"
        base_prompt += "Ensure ALL views show the EXACT SAME character with consistent features and style.\n"

        base_prompt += f"\nCRITICAL INSTRUCTION: Create character '{entity_name}' with ALL these SPECIFIC features. Do NOT blend or average features from reference images. Each character MUST be visually unique and easily distinguishable."

        # Negative prompt для предотвращения identity drift
        base_prompt += f"\n\nNEGATIVE (avoid): multiple characters, group shot, blended features from other characters, generic face, inconsistent features between views"
    
    else:  # location
        base_prompt = f"Create canonical reference image of location '{entity_name}'. "
        base_prompt += "Show ONLY this specific location WITHOUT any characters. "
        base_prompt += f"CRITICAL: Location '{entity_name}' must be VISUALLY DISTINCT and UNIQUE from other locations. "
        base_prompt += "Use reference images ONLY for style consistency (art style, color palette, lighting technique), NOT for location appearance. "
        base_prompt += f"Location '{entity_name}' must have these SPECIFIC and UNIQUE features:\n"
        
        # Динамически добавляем все атрибуты локации (кроме name и reference_image_path)
        # Структурируем атрибуты по важности
        for key, value in entity_data.items():
            if key in ["name", "reference_image_path", "location_sheet_instruction"]:
                continue
                
            if isinstance(value, list):
                # Для списков (key_objects, color_palette и т.д.)
                if value:  # Если список не пустой
                    items_text = ", ".join(str(item) for item in value)
                    field_name = key.replace('_', ' ').title()
                    base_prompt += f"\n- {field_name}: {items_text}"
            elif isinstance(value, str) and value.strip():
                # Для строковых значений
                field_name = key.replace('_', ' ').title()
                base_prompt += f"\n- {field_name}: {value}"
            elif isinstance(value, (int, float)):
                # Для числовых значений
                field_name = key.replace('_', ' ').title()
                base_prompt += f"\n- {field_name}: {value}"
        
        # === СПЕЦИАЛЬНАЯ ИНСТРУКЦИЯ ДЛЯ LOCATION SHEET ===
        base_prompt += "\n\nFORMAT INSTRUCTION: Create a LOCATION SHEET / ARCHITECTURAL VISUALIZATION. "
        base_prompt += "Split the image into 4 panels showing different angles of the SAME location:\n"
        base_prompt += "1. Top Left: Wide establishing shot\n"
        base_prompt += "2. Top Right: Cinematic low angle\n"
        base_prompt += "3. Bottom Left: Top-down floor plan view\n"
        base_prompt += "4. Bottom Right: Key detail or side view\n"
        base_prompt += "Ensure ALL panels show the EXACT SAME location with consistent lighting and style."
        
        base_prompt += f"\n\nCRITICAL INSTRUCTION: Create location '{entity_name}' with ALL these SPECIFIC features. Do NOT blend or average features from reference images. Each location MUST be visually unique and easily distinguishable."
    
    # Добавляем только релевантные правила
    relevant_rules = [rule["rule"] for rule in consistency_rules 
                     if entity_name in rule.get("applies_to", [])]
    
    if relevant_rules:
        rules_text = ". ".join(relevant_rules)
        base_prompt += f"Важные правила: {rules_text}."
    
    return base_prompt


def get_location_description_system_prompt(existing_summary_text: str) -> str:
    return f"""Ты - дизайнер локаций. На основе анализа сценария создай описание локации.

СУЩЕСТВУЮЩИЕ ЛОКАЦИИ (для стилистической согласованности):
{existing_summary_text}

СОЗДАЙ ОПИСАНИЕ в формате:
{{
  "name": "точное название",
  "description": "детальное описание (2-3 предложения), соответствующее контексту из сценария",
  "key_objects": ["объект1", "объект2", "объект3"],
  "atmosphere": "одно-два слова",
  "lighting": "описание освещения",
  "color_palette": ["#hex1", "#hex2", "#hex3", "#hex4"],
  "reference_image_path": "/references/locations/{{sanitized_name}}.png",
  "location_sheet_instruction": "Create a multi-view location sheet: Wide shot, Cinematic low angle, Top-down plan, Side view details."
}}

ТРЕБОВАНИЯ:
- Описание должно точно соответствовать тому, как локация используется в сценарии
- Стиль должен соответствовать общему визуальному стилю проекта
- Key_objects должны включать элементы, упомянутые в сценарии"""


def get_character_description_system_prompt(main_characters_summary_text: str) -> str:
    return f"""Ты - дизайнер персонажей. На основе анализа сценария создай описание персонажа.

ГЛАВНЫЕ ПЕРСОНАЖИ (для согласованности):
{main_characters_summary_text}

СОЗДАЙ ОПИСАНИЕ в формате:
{{
  "name": "точное имя",
  "age": "возраст или 'неопределенный'",
  "role": "роль в истории",
  "entity_nature": "human|anthropomorphic_animal|animal|robot|other",
  "species": "если не human/robot — вид (например: 'badger', 'fox', 'cat'); иначе пусто",
  "immutable_attributes": {{
    "face_shape": "форма лица",
    "eye_color": "цвет глаз", 
    "skin_tone": "тон кожи",
    "body_proportions": "пропорции тела",
    "unique_features": ["особенность1", "особенность2"]
  }},
  "variable_attributes": {{
    "base_clothing": "базовая одежда",
    "base_hairstyle": "прическа",
    "accessories": ["аксессуар1", "аксессуар2"]
  }},
  "reference_image_path": "/references/characters/{{sanitized_name}}.png",
  "gesture_set": ["жест1", "жест2", "жест3"],
  "speech_patterns": ["особенность речи1", "особенность речи2"],
  "no_go_rules": ["ограничение1", "ограничение2"]
}}

ТРЕБОВАНИЯ:
- Если персонаж говорит - опиши особенности речи на основе диалогов
- Если роль второстепенная - сделай описание менее детальным
- entity_nature заполняй строго по смыслу: если в сценарии явно "человек" — human; если антропоморфное животное — anthropomorphic_animal; если обычное животное — animal; если робот/андроид — robot; иначе other
- species указывай только для animal/anthropomorphic_animal (например: badger/fox/cat), иначе оставляй пустым
- Стиль должен соответствовать главным персонажам"""


SYSTEM_PROMPT_ANALYZE_MISSING_LOCATIONS = """Ты - аналитик сценариев. Проанализируй сгенерированный сценарий и существующие локации.

ЗАДАЧА:
Найди ВСЕ локации, упомянутые в сценарии, которых НЕТ в списке существующих локаций.
Учитывай синонимы, вариации названий и косвенные упоминания.

Примеры сопоставлений:
- "ЭКСТ. ВЫЖЖЕННАЯ ПУСТЫНЯ - ДЕНЬ" = новая локация, если в библии есть только "Серверная фабрика"
- "ИНТ. СЕРВЕРНАЯ - НОЧЬ" = существующая "Серверная фабрика 'ГлобалСёрч'"
- "космический корабль" в тексте action = новая локация "Космический корабль"

ВЕРНИ JSON:
{
  "new_locations": [
    {
      "name": "точное название из сценария",
      "context": "контекст упоминания из сценария",
      "scene_references": ["номера сцен где упоминается"]
    }
  ],
  "existing_matches": [
    {
      "screenplay_mention": "как упомянуто в сценарии", 
      "existing_location": "название из библии"
    }
  ]
}"""


SYSTEM_PROMPT_ANALYZE_MISSING_CHARACTERS = """Ты - аналитик персонажей. Проанализируй сгенерированный сценарий и существующих персонажей.

ЗАДАЧА:
Найди ВСЕХ персонажей, упомянутых в сценарии, которых НЕТ в списке существующих персонажей.
Учитывай:
- Прямые упоминания в массиве "characters"
- Персонажей в диалогах
- Косвенные упоминания в тексте action
- Вариации имён (уменьшительные, прозвища, сокращения одного и того же персонажа)

Примеры:
- Голосовые персонажи в диалоге (например "ГОЛОС СИСТЕМЫ") = новый персонаж, если его нет в библии
- Безымянные роли в action (например "охранник", "продавец") = новый персонаж
- Персонаж, уже присутствующий в библии под любой вариацией имени = существующий персонаж

ВЕРНИ JSON:
{
  "new_characters": [
    {
      "name": "точное имя",
      "role": "роль в сценарии",
      "context": "контекст появления",
      "scene_references": ["номера сцен где упоминается"],
      "has_dialogue": true/false
    }
  ],
  "existing_matches": [
    {
      "screenplay_mention": "как упомянуто в сценарии",
      "existing_character": "имя из библии"
    }
  ]
}"""

