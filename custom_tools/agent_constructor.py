from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
import re

import yaml
from utils import call_openai_api


@dataclass
class Spec:
    agent_name: str
    persona: str
    goals: List[str]
    capabilities: List[str]
    inputs: List[Dict[str, Any]]
    outputs: List[Dict[str, Any]]
    constraints: List[str]
    data_sources: List[Dict[str, Any]]
    quality_metrics: List[Dict[str, Any]]
    notes: Optional[str] = None


def parse_spec(description: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Преобразует текстовое описание в нормализованный spec (без выбора тулов).

    Args:
        description (str): Текстовое описание агента (RU/EN).
        context (Optional[Dict[str, Any]]): Дополнительный контекст/политики (необязательно).

    Returns:
        Dict[str, Any]: Нормализованная спецификация агента со структурами
            agent_name, persona, goals, capabilities, inputs, outputs,
            constraints, data_sources, quality_metrics, notes.

    Raises:
        ValueError: Если описание пустое или некорректное.
    """
    if not isinstance(description, str) or not description.strip():
        raise ValueError("INVALID_SPEC: description is empty")

    sys_prompt = (
        "Ты помощник, который из текстового описания строит строгий JSON-спек агента. "
        "Верни ТОЛЬКО валидный JSON без комментариев и лишнего текста. \n"
        "Схема ключей: {\n"
        "  'agent_name': str, 'persona': str,\n"
        "  'goals': [str], 'capabilities': [str],\n"
        "  'inputs': [{'name': str, 'type': str, 'required': bool}],\n"
        "  'outputs': [{'name': str, 'type': str}],\n"
        "  'constraints': [str],\n"
        "  'data_sources': [{'type': 'db|api|file|rag', 'id': str}],\n"
        "  'quality_metrics': [{'name': str, 'target': str|number}],\n"
        "  'notes': str|null\n"
        "}. Все поля должны присутствовать. Пустые списки допустимы.\n"
        "agent_name — латинский с подчёркиваниями, без пробелов, <= 40 символов."
    )

    user_prompt = (
        "Описание агента:\n" + description.strip() + "\n\n" +
        "Контекст (необязательно):\n" + json.dumps(context or {}, ensure_ascii=False)
    )

    raw = call_openai_api(
        prompt=user_prompt,
        system_prompt=sys_prompt,
        max_tokens=1200,
        temperature=0.0,
        response_format={"type": "json_object"},
    )

    def _extract_json(s: str) -> Dict[str, Any]:
        if not s:
            return {}
        # Быстрая попытка прямого парсинга
        try:
            return json.loads(s)
        except Exception:
            pass
        # Попытка вырезать крупнейший JSON-блок
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = s[start:end + 1]
            try:
                return json.loads(candidate)
            except Exception:
                pass
        return {}

    data = _extract_json(raw)
    if not isinstance(data, dict):
        data = {}

    # Нормализация и заполнение обязательных полей
    def _safe_str(v: Any, default: str = "") -> str:
        return str(v)[:200] if v is not None else default

    def _sanitize_agent_name(name: str) -> str:
        if not isinstance(name, str):
            name = "generated_agent"
        # lower, spaces -> underscore
        name = name.lower().replace(" ", "_")
        # remove non [a-z0-9_]
        name = re.sub(r"[^a-z0-9_]", "", name)
        # collapse multiple underscores
        name = re.sub(r"_+", "_", name)
        # trim underscores
        name = name.strip("_")
        # enforce length
        if len(name) == 0:
            name = "generated_agent"
        return name[:40]

    agent_name = _sanitize_agent_name(_safe_str(data.get("agent_name")) or (context or {}).get("agent_name", "generated_agent"))

    spec = Spec(
        agent_name=agent_name,
        persona=_safe_str(data.get("persona"), "Generated agent"),
        goals=[_safe_str(x) for x in (data.get("goals") or []) if str(x).strip()],
        capabilities=[_safe_str(x) for x in (data.get("capabilities") or []) if str(x).strip()],
        inputs=[
            {
                "name": _safe_str(i.get("name"), "task"),
                "type": _safe_str(i.get("type"), "string"),
                "required": bool(i.get("required", True)),
            }
            for i in (data.get("inputs") or []) if isinstance(i, dict)
        ] or [{"name": "task", "type": "string", "required": True}],
        outputs=[
            {
                "name": _safe_str(o.get("name", "result")),
                "type": _safe_str(o.get("type", "string")),
            }
            for o in (data.get("outputs") or []) if isinstance(o, dict)
        ] or [{"name": "result", "type": "string"}],
        constraints=[_safe_str(x) for x in (data.get("constraints") or []) if str(x).strip()],
        data_sources=[
            {
                "type": _safe_str(s.get("type", "")),
                "id": _safe_str(s.get("id", "")),
            }
            for s in (data.get("data_sources") or []) if isinstance(s, dict)
        ],
        quality_metrics=[
            {
                "name": _safe_str(m.get("name", "")),
                "target": m.get("target", ""),
            }
            for m in (data.get("quality_metrics") or []) if isinstance(m, dict)
        ],
        notes=_safe_str(data.get("notes"), None) or None,
    )

    return asdict(spec)


def _load_custom_tool_names() -> List[str]:
    """Считывает имена custom‑тулов из tool_definitions/*.yaml без импорта кода."""
    tool_dir = "tool_definitions"
    names: List[str] = []
    if not os.path.isdir(tool_dir):
        return names
    for fname in os.listdir(tool_dir):
        if not fname.endswith(".yaml"):
            continue
        try:
            with open(os.path.join(tool_dir, fname), "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                name = data.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
        except Exception:
            continue
    return names


def _load_mcp_tool_names() -> List[str]:
    """Возвращает имена MCP‑инструментов, уже загруженных проектом.

    Returns:
        List[str]: Список имён инструментов из `mcp_tools.mcp_tools`.
    """
    try:
        from mcp_tools import mcp_tools  # type: ignore
    except Exception:
        return []
    names: List[str] = []
    for t in mcp_tools:
        name = getattr(t, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


def validate_tools_availability(tools_requested: List[str], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Проверяет доступность запрошенных инструментов по именам (без префиксов).

    Поддерживается обратная совместимость: префиксы `custom:`/`mcp:`
    автоматически удаляются при поиске и сохраняются как примечание.

    Args:
        tools_requested (List[str]): Список имён инструментов (можно с префиксами, будут очищены).
        context (Optional[Dict[str, Any]]): Контекст/политики (необязательно).

    Returns:
        Dict[str, Any]: Объект с полями:
            - tools_resolved: список подтверждённых инструментов с типом `custom` или `mcp` и именем.
            - unavailable_tools: список недоступных инструментов с причиной.
    """
    tools_resolved: List[Dict[str, Any]] = []
    unavailable: List[Dict[str, Any]] = []

    custom_names = set(_load_custom_tool_names())
    mcp_names = set(_load_mcp_tool_names())

    for raw in tools_requested:
        if not isinstance(raw, str) or not raw.strip():
            unavailable.append({"tool": raw, "reason": "INVALID_NAME"})
            continue

        original = raw.strip()
        note = None
        enforced_type: Optional[str] = None
        name = original
        # Обрабатываем префиксы как явную дисамбигуацию
        if name.startswith("custom:"):
            enforced_type = "custom"
            name = name.split(":", 1)[1]
            note = "provided with custom: prefix"
        elif name.startswith("mcp:"):
            enforced_type = "mcp"
            name = name.split(":", 1)[1]
            # mcp:server/tool → берём tool
            if "/" in name:
                name = name.split("/", 1)[1]
            note = "provided with mcp: prefix"

        in_custom = name in custom_names
        in_mcp = name in mcp_names

        if enforced_type == "custom":
            if in_custom:
                tools_resolved.append({"type": "custom", "name": name, "id": name, "note": note})
            else:
                unavailable.append({"tool": original, "reason": "NOT_FOUND_IN_CUSTOM"})
            continue
        if enforced_type == "mcp":
            if in_mcp:
                tools_resolved.append({"type": "mcp", "name": name, "id": name, "note": note})
            else:
                unavailable.append({"tool": original, "reason": "NOT_FOUND_IN_MCP"})
            continue

        if in_custom and in_mcp:
            unavailable.append({"tool": original, "reason": "AMBIGUOUS_TOOL_NAME(custom_and_mcp)", "hint": "уточните custom: или mcp:"})
        elif in_custom:
            tools_resolved.append({"type": "custom", "name": name, "id": name, "note": note})
        elif in_mcp:
            tools_resolved.append({"type": "mcp", "name": name, "id": name, "note": note})
        else:
            unavailable.append({"tool": original, "reason": "NOT_FOUND_IN_CUSTOM_OR_MCP"})

    return {"tools_resolved": tools_resolved, "unavailable_tools": unavailable}


def plan_dependencies(tools_resolved: List[Dict[str, Any]], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Формирует простой план зависимостей/конфигураций без побочных эффектов.

    Args:
        tools_resolved (List[Dict[str, Any]]): Список подтверждённых инструментов из валидатора.
        context (Optional[Dict[str, Any]]): Контекст/политики (необязательно).

    Returns:
        Dict[str, Any]: План с полями tools_ordered, graph, configs,
        external_services, risks, actions_required.
    """
    nodes = [t.get("id") or t.get("name") for t in tools_resolved]
    graph = {"nodes": nodes, "edges": []}
    configs = []
    for t in tools_resolved:
        configs.append({
            "tool_id": t.get("id") or t.get("name"),
            "config_schema_ref": None,
            "required_secrets": [],
            "env_bindings": {},
        })

    plan = {
        "tools_ordered": nodes,
        "graph": graph,
        "configs": configs,
        "external_services": [],
        "risks": [],
        "actions_required": [],
    }
    return plan


def generate_agent_profile(spec: Dict[str, Any], tools_resolved: List[Dict[str, Any]], dependency_plan: Dict[str, Any], output_dir: str = "agent_profiles") -> str:
    """Формирует YAML‑профиль агента на основе входных данных.

    Args:
        spec (Dict[str, Any]): Нормализованный spec из `parse_spec`.
        tools_resolved (List[Dict[str, Any]]): Подтверждённые инструменты из валидатора доступности.
        dependency_plan (Dict[str, Any]): План зависимостей из `plan_dependencies`.
        output_dir (str, optional): Каталог для записи профиля. По умолчанию `agent_profiles`.

    Returns:
        str: Абсолютный или относительный путь к созданному YAML‑файлу профиля агента.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Санитизируем и гарантируем уникальность имени файла
    name_raw = spec.get("agent_name") or "generated_agent"
    name_sanitized = re.sub(r"[^a-z0-9_]", "", (name_raw.lower().replace(" ", "_")))
    name_sanitized = re.sub(r"_+", "_", name_sanitized).strip("_") or "generated_agent"
    name_sanitized = name_sanitized[:40]
    profile_path = os.path.join(output_dir, f"{name_sanitized}.yaml")
    if os.path.exists(profile_path):
        suffix = 1
        base = name_sanitized
        while os.path.exists(profile_path) and suffix < 1000:
            profile_path = os.path.join(output_dir, f"{base}_{suffix}.yaml")
            suffix += 1

    tools_in_profile: List[str] = [t.get("name") for t in tools_resolved]

    # Генерация промпта и описания при помощи LLM, учитывая инструменты
    def _get_tools_info_text() -> str:
        """Возвращает сводку по инструментам (имя + описание), custom + MCP."""
        info_lines: List[str] = []
        # custom
        from pathlib import Path as _Path
        import yaml as _yaml
        tool_dir = _Path("tool_definitions")
        custom_map: Dict[str, str] = {}
        if tool_dir.exists():
            for f in tool_dir.glob("*.yaml"):
                try:
                    with open(f, 'r', encoding='utf-8') as _f:
                        data = _yaml.safe_load(_f) or {}
                        n = data.get('name')
                        d = data.get('description')
                        if isinstance(n, str) and n:
                            custom_map[n] = d or ''
                except Exception:
                    continue
        # mcp
        mcp_map: Dict[str, str] = {}
        try:
            from mcp_tools import mcp_tools as _mcp_tools
            for t in _mcp_tools:
                n = getattr(t, 'name', None)
                d = getattr(t, 'description', None)
                if isinstance(n, str) and n:
                    mcp_map[n] = d or ''
        except Exception:
            pass

        for t in tools_in_profile:
            desc = custom_map.get(t) or mcp_map.get(t) or ''
            if desc:
                info_lines.append(f"- {t}: {desc}")
            else:
                info_lines.append(f"- {t}")
        return "\n".join(info_lines) if info_lines else "- Нет специальных инструментов"

    tools_info_text = _get_tools_info_text()

    # Синтез системного промпта
    prompt_compose_system = (
        "Ты конструктор промптов для ИИ‑агентов.\n"
        "Сгенерируй системный промпт (на русском), который:\n"
        "- Ясно задаёт роль/персону агента и его цели;\n"
        "- Учитывает доступные инструменты (как их звать, когда звать, ограничения);\n"
        "- Описывает входы/выходы агента и ожидаемый формат ответов;\n"
        "- Включает правила безопасности и запрос уточнений при нехватке данных;\n"
        "- Кратко, структурированно, БЕЗ лишнего текста. Верни ТОЛЬКО текст промпта. Не пиши, что это системный промпт."
    )
    prompt_compose_user = (
        f"Персона: {spec.get('persona','')}\n"
        f"Цели: {', '.join(spec.get('goals', []))}\n"
        f"Входы: {json.dumps(spec.get('inputs', []), ensure_ascii=False)}\n"
        f"Выходы: {json.dumps(spec.get('outputs', []), ensure_ascii=False)}\n"
        f"Ограничения: {', '.join(spec.get('constraints', [])) or '—'}\n"
        f"Инструменты:\n{tools_info_text}\n"
        "Сформируй итоговый системный промпт."
    )
    prompt_text = call_openai_api(
        prompt=prompt_compose_user,
        system_prompt=prompt_compose_system,
        max_tokens=1600,
        temperature=0.2,
    ) or f"Роль: {spec.get('persona', '')}. Цели: {', '.join(spec.get('goals', []))}"

    # Нормализация текста промпта: убираем маркеры и экранированные \n
    def _clean_prompt_text(s: str) -> str:
        if not isinstance(s, str):
            return ""
        s = s.strip()
        # Превращаем литералы \r\n и \n в реальные переводы строк
        s = s.replace("\\r\\n", "\n").replace("\\n", "\n")
        # Удаляем возможные заголовки вроде "Системный промпт:" (с жирным и без)
        s = re.sub(r"^(\*\*)?\s*Системный\s+промпт\s*:?\s*(\*\*)?\n?", "", s, flags=re.IGNORECASE)
        return s

    prompt_text = _clean_prompt_text(prompt_text)

    # Синтез краткого описания
    desc_system = (
        "Ты помогаешь формулировать краткое (2–3 предложения) описательное резюме агента на русском.\n"
        "Укажи назначение, ключевые способности и упоминание инструментов. Верни только текст описания."
    )
    desc_user = (
        f"Имя: {spec.get('agent_name','')}\n"
        f"Персона: {spec.get('persona','')}\n"
        f"Цели: {', '.join(spec.get('goals', []))}\n"
        f"Инструменты:\n{tools_info_text}"
    )
    description_text = call_openai_api(
        prompt=desc_user,
        system_prompt=desc_system,
        max_tokens=400,
        temperature=0.3,
    ) or (spec.get("persona") or "Generated agent")
    if isinstance(description_text, str):
        description_text = description_text.strip().replace("\\r\\n", " ").replace("\\n", " ")

    # Готовим структуру профиля
    profile_yaml: Dict[str, Any] = {
        "type": "tool_calling",
        "description": description_text.strip(),
        "model": "model_code",
        "tools": tools_in_profile,
        "memory_policy": {"provide_run_summary": True},
        "prompt_templates": prompt_text.strip(),
        "enable": True,
    }

    # Пишем YAML с литеральным блоком для prompt_templates
    try:
        from ruamel.yaml import YAML  # type: ignore
        from ruamel.yaml.scalarstring import LiteralScalarString  # type: ignore

        yaml_writer = YAML()
        yaml_writer.preserve_quotes = True
        yaml_writer.width = 4096

        profile_yaml["prompt_templates"] = LiteralScalarString(profile_yaml["prompt_templates"])  # гарантируем '|'

        with open(profile_path, "w", encoding="utf-8") as f:
            yaml_writer.dump(profile_yaml, f)
    except Exception:
        # Fallback на PyYAML с кастомным представителем
        class LiteralStr(str):
            pass

        class CustomDumper(yaml.SafeDumper):
            pass

        def _repr_literal_str(dumper, data):
            return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')

        CustomDumper.add_representer(LiteralStr, _repr_literal_str)
        profile_yaml["prompt_templates"] = LiteralStr(profile_yaml["prompt_templates"])
        with open(profile_path, "w", encoding="utf-8") as f:
            yaml.dump(profile_yaml, f, allow_unicode=True, sort_keys=False, Dumper=CustomDumper)

    return profile_path


def construct_agent(description: str, tools_requested: List[str], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Основной сценарий оркестратора: 1) parse spec, 2) validate tools, 3) plan, 4) generate profile."""
    spec = parse_spec(description, context)
    availability = validate_tools_availability(tools_requested, context)
    if availability.get("unavailable_tools"):
        return {
            "ok": False,
            "reason": "UNAVAILABLE_TOOLS",
            "details": availability,
        }
    plan = plan_dependencies(availability.get("tools_resolved", []), context)
    profile_path = generate_agent_profile(spec, availability.get("tools_resolved", []), plan)
    return {
        "ok": True,
        "profile_path": profile_path,
        "spec": spec,
        "plan": plan,
    }
