"""Проверка артефактов storybook по JSON Schema + смысловые проверки.

Схемы мягкие: лишние поля разрешены (пайплайн добавляет много служебных),
обязательно только то, без чего падает следующий шаг. «Желательные» поля
перечислены в собственном ключевом слове `x-recommended`: их отсутствие даёт
предупреждение, а не ошибку.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

_DIR = Path(__file__).parent

_TYPE_NAMES = {
    "object": "объект", "array": "список", "string": "строка", "integer": "целое число",
    "number": "число", "boolean": "да/нет", "null": "пусто",
}


@functools.lru_cache(maxsize=None)
def load_schema(key: str) -> dict:
    path = _DIR / f"{key}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def schema_keys() -> list[str]:
    return sorted(p.name[: -len(".schema.json")] for p in _DIR.glob("*.schema.json"))


def pointer_of(path) -> str:
    return "".join("/" + str(seg).replace("~", "~0").replace("/", "~1") for seg in path)


def label_of(key: str, path) -> str:
    """Человеческий путь: «Шоты · элемент 4 · Тайминг» (title из схемы, иначе имя поля)."""
    current: Any = load_schema(key)
    parts = [current.get("title") or ""]
    for seg in path:
        if isinstance(seg, int):
            parts.append(f"элемент {seg + 1}")
            current = current.get("items") if isinstance(current, dict) else None
        else:
            sub = (current.get("properties") or {}).get(seg) if isinstance(current, dict) else None
            parts.append((sub or {}).get("title") or str(seg))
            current = sub
    unique = [p for idx, p in enumerate(parts) if p and (idx == 0 or p != parts[idx - 1])]
    return " · ".join(unique)


def make_finding(key: str, severity: str, code: str, path, message: str,
                 line: int | None = None, column: int | None = None) -> dict:
    path = list(path)
    finding = {
        "severity": severity, "code": code, "path": path, "pointer": pointer_of(path),
        "label": label_of(key, path), "message": message,
    }
    if code == "syntax":
        finding["line"] = line
        finding["column"] = column
    return finding


def _type_name(value: Any) -> str:
    if value is None:
        return _TYPE_NAMES["null"]
    if isinstance(value, bool):
        return _TYPE_NAMES["boolean"]
    if isinstance(value, int):
        return _TYPE_NAMES["integer"]
    if isinstance(value, float):
        return _TYPE_NAMES["number"]
    if isinstance(value, str):
        return _TYPE_NAMES["string"]
    if isinstance(value, list):
        return _TYPE_NAMES["array"]
    return _TYPE_NAMES["object"]


def _translate(err) -> tuple[str, str]:
    """Тип ошибки jsonschema -> (код, русское сообщение)."""
    validator, expected = err.validator, err.validator_value
    if validator == "type":
        names = expected if isinstance(expected, list) else [expected]
        want = " или ".join(_TYPE_NAMES.get(n, n) for n in names)
        return "type", f"Ожидается {want}, сейчас {_type_name(err.instance)}"
    if validator == "enum":
        return "enum", "Допустимо: " + ", ".join(str(v) for v in expected)
    if validator == "const":
        return "enum", f"Допустимо только: {expected}"
    if validator == "minimum":
        return "range", f"Слишком мало: не меньше {expected}"
    if validator == "exclusiveMinimum":
        return "range", f"Должно быть больше {expected}"
    if validator == "maximum":
        return "range", f"Слишком много: не больше {expected}"
    if validator == "minLength":
        return "range", f"Слишком короткое значение: не меньше {expected} симв."
    if validator == "maxLength":
        return "range", f"Слишком длинное значение: не больше {expected} симв."
    if validator == "minItems":
        return "range", f"Слишком мало элементов: не меньше {expected}"
    if validator == "maxItems":
        return "range", f"Слишком много элементов: не больше {expected}"
    if validator == "pattern":
        return "pattern", (err.schema.get("x-message") if isinstance(err.schema, dict) else None) or "Значение не подходит по формату"
    if validator == "format":
        return "format", "Значение не подходит по формату"
    return "custom", err.message


def _path_key(path) -> tuple:
    return tuple((0, seg, "") if isinstance(seg, int) else (1, 0, str(seg)) for seg in path)


def sort_findings(findings: list[dict]) -> list[dict]:
    """Сначала syntax/root, затем по пути (ошибки раньше предупреждений на одном пути)."""
    return sorted(
        findings,
        key=lambda f: (f["code"] not in ("syntax", "root"), _path_key(f["path"]), f["severity"] != "error"),
    )


def _recommended(key: str, schema: Any, data: Any, path: list, out: list) -> None:
    if not isinstance(schema, dict):
        return
    if isinstance(data, dict):
        props = schema.get("properties") or {}
        for name in schema.get("x-recommended") or []:
            if name not in data:
                title = (props.get(name) or {}).get("title") or name
                out.append(make_finding(key, "warning", "recommended", path + [name],
                                        f"Рекомендуется заполнить поле «{title}»"))
        for name, sub in props.items():
            if name in data:
                _recommended(key, sub, data[name], path + [name], out)
    elif isinstance(data, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for idx, element in enumerate(data):
                _recommended(key, items, element, path + [idx], out)


def validate_structure(key: str, data: Any) -> list[dict]:
    """Нарушения схемы (error) и отсутствие желательных полей (warning)."""
    from jsonschema.exceptions import best_match

    schema = load_schema(key)
    findings: list[dict] = []
    required_seen: set[tuple] = set()
    for err in Draft202012Validator(schema).iter_errors(data):
        if err.context:
            err = best_match(err.context) or err
        path = list(err.absolute_path)
        if err.validator == "required":
            if tuple(path) in required_seen:
                continue
            required_seen.add(tuple(path))
            props = (err.schema.get("properties") or {}) if isinstance(err.schema, dict) else {}
            for name in err.validator_value:
                if isinstance(err.instance, dict) and name not in err.instance:
                    title = (props.get(name) or {}).get("title") or name
                    findings.append(make_finding(key, "error", "required", path + [name],
                                                 f"Не заполнено обязательное поле «{title}»"))
            continue
        code, message = _translate(err)
        if code == "type" and not path:
            code = "root"
        findings.append(make_finding(key, "error", code, path, message))
    _recommended(key, schema, data, [], findings)
    return findings


class CheckContext:
    """Что нужно смысловым проверкам: папка проекта и чтение соседних артефактов."""

    def __init__(self, project_dir: Path | None = None,
                 read_artifact: Callable[[str], Any] | None = None):
        self.project_dir = Path(project_dir) if project_dir else None
        self._read = read_artifact

    def read(self, key: str) -> Any:
        if self._read is None:
            return None
        try:
            return self._read(key)
        except Exception:
            return None


def validate_all(key: str, data: Any, ctx: CheckContext | None = None) -> list[dict]:
    """Схема + смысловые проверки, отсортировано. Без ограничения числа находок."""
    from . import checks

    findings = validate_structure(key, data)
    if not any(f["code"] == "root" for f in findings):
        findings.extend(checks.run_checks(key, data, ctx or CheckContext(),
                                          lambda *a, **k: make_finding(key, *a, **k)))
    return sort_findings(findings)
