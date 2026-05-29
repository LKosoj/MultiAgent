# Глава 15: Инструменты Конструктора Агентов

Набор инструментов для метагента `agent_constructor`, который создаёт новых агентов «под задачу».

## Последовательность
1) spec_parser — нормализует текст в JSON-спецификацию.
2) tools_availability_validator — проверяет наличие запрошенных инструментов (custom/MCP).
3) dependency_planner — формирует простой план/порядок (задел для расширения).
4) agent_profile_generator — генерирует description/prompt и собирает YAML-профиль.

## 1. spec_parser
```python
def parse_spec(description: str) -> dict:
    sys = "Верни ТОЛЬКО JSON по схеме {...}"
    raw = call_openai_api(prompt=description, system_prompt=sys)
    return normalize(raw)
```
Результат: `{agent_name, persona, goals, inputs, outputs, ...}`

## 2. tools_availability_validator
```python
def validate_tools_availability(tools: list[str]) -> dict:
    custom = set(_load_custom_tool_names())
    mcp = set(_load_mcp_tool_names())
    resolved, missing = [], []
    for t in tools:
        if t in custom: resolved.append({"type": "custom", "name": t})
        elif t in mcp: resolved.append({"type": "mcp", "name": t})
        else: missing.append({"tool": t, "reason": "NOT_FOUND"})
    return {"tools_resolved": resolved, "unavailable_tools": missing}
```
Гарантирует, что профиль не будет ссылаться на несуществующие инструменты.

## 3. dependency_planner (упрощённый)
```python
def plan_dependencies(tools_resolved: list[dict]) -> dict:
    nodes = [t["name"] for t in tools_resolved]
    return {"tools_ordered": nodes, "graph": {"nodes": nodes, "edges": []}}
```
Подготовка для будущих сложных сценариев.

## 4. agent_profile_generator
```python
def generate_agent_profile(spec: dict, tools_resolved: list[dict]) -> str:
    prompt = call_openai_api(...)
    desc = call_openai_api(...)
    profile = {
        "type": "tool_calling",
        "description": desc,
        "model": "model_code",
        "tools": [t["name"] for t in tools_resolved],
        "prompt_templates": prompt,
        "enable": True,
    }
    path = f"agent_profiles/{spec['agent_name']}.yaml"
    save_yaml(path, profile)
    return path
```
Формирует финальный YAML-файл и сохраняет его в `agent_profiles/`.
