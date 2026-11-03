"""Модель экрана «Файлы» (без tkinter): строки списка, состояние редактора."""

from .files_texts import FILES_TEXTS as T

_STATUS = {"present": "status_present", "missing": "status_missing", "invalid": "status_invalid"}


def list_rows(items):
    """Строки списка: подпись, статус словом, пометки текстом, путь для подсказки."""
    rows = []
    for it in items:
        marks = [T["read_only"]] if not it.get("editable", True) else []
        if it.get("careful"):
            marks.append(T["careful"])
        rows.append({"key": it["artifact_key"], "title": it["title"], "status": T[_STATUS.get(it.get("status"), "status_missing")],
                     "marks": marks, "path": it["path"], "item": it})
    return rows


def row_text(row):
    return row["title"] + (f"  [{', '.join(row['marks'])}]" if row["marks"] else "")


class EditorState:
    """Состояние одного открытого файла: отпечаток, правки, режим."""

    def __init__(self, key, read):
        self.key = key
        self.title = read.get("title", key)
        self.path = read.get("path", "")
        self.digest = read.get("digest")
        self.exists = read.get("exists", True)
        self.editable = bool(read.get("editable", True))
        self.read_only_reason = read.get("read_only_reason", "")
        self.parse_error = read.get("parse_error")
        self.run_state = read.get("run_state", "free")
        self.run_state_message = read.get("run_state_message", "")
        self.content = read.get("content")
        self.text = read.get("text")
        self.dirty = False
        self.my_edits = None  # буфер «Мои правки» после конфликта

    @property
    def can_edit(self):
        return self.editable and self.run_state != "busy" and self.exists


def read_only_note(state, forbidden=False):
    if forbidden:
        return T["forbidden"]
    if not state.editable:
        return f"{T['read_only']}: {state.read_only_reason}" if state.read_only_reason else T["read_only"]
    if state.run_state == "busy":
        return state.run_state_message or T["busy"]
    return ""


def broken_text(state):
    err = state.parse_error or {}
    reason = err.get("message", "")
    if err.get("line"):
        reason = f"строка {err['line']}: {reason}"
    return T["broken"].format(name=state.title, reason=reason)


def counts_text(counts):
    return T["findings_title"].format(errors=counts.get("errors", 0), warnings=counts.get("warnings", 0))


def finding_text(f):
    return f"{f.get('label') or f.get('pointer') or ''} · {f.get('message', '')}"


# --- то же, что в artifactForm.ts и filesView.ts (эталон golden_files_*_vectors.json) ---

import json
import re

from pathlib import Path

LONG_STRING_LIMIT = 80
SCHEMAS = Path(__file__).resolve().parents[3] / "custom_tools/storybook/studio/schemas"
_labels_cache = None
_NUMBER_RE = re.compile(r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$")
_WS = " \t\n\r\f\v ﻿"


def _walk(node, pointer, out):
    if not isinstance(node, dict):
        return
    title = node.get("title")
    if isinstance(title, str) and title:
        out[pointer] = title
    for name, sub in (node.get("properties") or {}).items():
        _walk(sub, f"{pointer}/{name.replace('~', '~0').replace('/', '~1')}", out)
    if isinstance(node.get("items"), dict):
        _walk(node["items"], f"{pointer}/*", out)


def artifact_labels():
    """Таблица подписей из схем studio (только чтение): {ключ файла: {шаблон pointer: подпись}}."""
    global _labels_cache
    if _labels_cache is None:
        labels = {}
        for path in sorted(SCHEMAS.glob("*.schema.json")):
            out = {}
            _walk(json.loads(path.read_text(encoding="utf-8")), "", out)
            labels[path.name[: -len(".schema.json")]] = out
        _labels_cache = labels
    return _labels_cache


def _esc(part):
    return str(part).replace("~", "~0").replace("/", "~1")


def artifact_labels():
    """Таблица подписей из схем studio (только чтение): {ключ файла: {шаблон pointer: подпись}}."""
    global _labels_cache
    if _labels_cache is None:
        labels = {}
        for path in sorted(SCHEMAS.glob("*.schema.json")):
            out = {}
            _walk(json.loads(path.read_text(encoding="utf-8")), "", out)
            labels[path.name[: -len(".schema.json")]] = out
        _labels_cache = labels
    return _labels_cache


def pointer_of(path):
    return "".join("/" + _esc(p) for p in path)


def template_of(path):
    return "".join("/*" if isinstance(p, int) else "/" + _esc(p) for p in path)


def is_long_string(value):
    return len(value) > LONG_STRING_LIMIT or "\n" in value


def label_for(artifact_key, path):
    """Подпись поля из таблицы схем; у элемента массива - «<подпись> N» или «Элемент N»."""
    known = artifact_labels().get(artifact_key, {}).get(template_of(path))
    last = path[-1] if path else None
    if isinstance(last, int) and not isinstance(last, bool):
        return f"{known} {last + 1}" if known else f"Элемент {last + 1}"
    if known:
        return known
    return "" if last is None else str(last)


def parse_number(text):
    """Число из текста поля; запятая считается десятичным разделителем."""
    normalized = text.strip().replace(",", ".", 1)
    if not _NUMBER_RE.match(normalized):
        return None
    n = float(normalized)
    return int(n) if n.is_integer() and abs(n) < 1e15 else n


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _classify(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if _is_num(value):
        return "number"
    if isinstance(value, str):
        return "text" if is_long_string(value) else "string"
    if isinstance(value, list):
        if not value or all(isinstance(v, str) for v in value):
            return "lines"
        if all(_is_num(v) for v in value):
            return "numlines"
        if all(isinstance(v, dict) for v in value):
            return "list"
        return "raw"
    return "group" if isinstance(value, dict) else "raw"


def build_fields(artifact_key, content):
    """Плоский список полей формы; None - форма недоступна (корень не объект и не массив)."""
    out = []

    def make(path, kind, value, depth, read_only=False):
        return {"pointer": pointer_of(path), "path": path, "label": label_for(artifact_key, path), "kind": kind,
                "value": value, "readOnly": read_only, "depth": depth, "size": 0}

    def emit(path, value, depth):
        kind = _classify(value)
        field = make(path, kind, None if kind in ("group", "list") else value, depth, kind == "raw")
        out.append(field)
        index = len(out) - 1
        if kind == "group":
            for name, sub in value.items():
                emit([*path, name], sub, depth + 1)
        elif kind == "list":
            for i, element in enumerate(value):
                item_path = [*path, i]
                item = make(item_path, "item", None, depth + 1)
                out.append(item)
                item_index = len(out) - 1
                for name, sub in element.items():
                    emit([*item_path, name], sub, depth + 2)
                item["size"] = len(out) - item_index - 1
        field["size"] = len(out) - index - 1

    if isinstance(content, dict):
        for name, sub in content.items():
            emit([name], sub, 0)
        return out
    if isinstance(content, list):
        emit([], content, 0)
        out[0]["label"] = artifact_labels().get(artifact_key, {}).get("", "")
        return out
    return None


def _set_at(content, path, value):
    if not path:
        return value
    head, rest = path[0], path[1:]
    if isinstance(content, list) and isinstance(head, int):
        copy = list(content)
        copy[head] = _set_at(content[head], rest, value)
        return copy
    if isinstance(content, dict) and isinstance(head, str):
        return {**content, head: _set_at(content.get(head), rest, value)}
    return content


def _lines(text):
    return [line for line in re.split(r"\r?\n", text) if line.strip() != ""]


def apply_input(artifact_key, content, pointer, value):
    """Применить ввод к документу по указателю поля; не число - ошибка, значение не меняется."""
    fields = build_fields(artifact_key, content) or []
    field = next((f for f in fields if f["pointer"] == pointer), None)
    if field is None:
        return {"content": content, "error": None}
    kind, path = field["kind"], field["path"]
    same = {"content": content, "error": None}
    fail = {"content": content, "error": T["field_number"].replace("{name}", field["label"])}
    if kind == "bool":
        return {"content": _set_at(content, path, value), "error": None} if isinstance(value, bool) else same
    if not isinstance(value, str):
        return same
    if kind in ("string", "text"):
        return {"content": _set_at(content, path, value), "error": None}
    if kind == "null":
        return {"content": _set_at(content, path, None if value == "" else value), "error": None}
    if kind == "number":
        n = parse_number(value)
        return fail if n is None else {"content": _set_at(content, path, n), "error": None}
    if kind == "lines":
        return {"content": _set_at(content, path, _lines(value)), "error": None}
    if kind == "numlines":
        numbers = []
        for line in _lines(value):
            n = parse_number(line)
            if n is None:
                return fail
            numbers.append(n)
        return {"content": _set_at(content, path, numbers), "error": None}
    return same


def content_to_text(content):
    return json.dumps(content, ensure_ascii=False, indent=2)


def line_col_at(text, offset):
    head = text[:max(0, min(offset, len(text)))]
    lines = head.split("\n")
    return {"line": len(lines), "column": len(lines[-1]) + 1}


def offset_at(text, line, column):
    lines = text.split("\n")
    row = min(max(line, 1), len(lines))
    offset = sum(len(lines[i]) + 1 for i in range(row - 1))
    return offset + min(max(column, 1) - 1, len(lines[row - 1]))


def _skip_ws(t, i):
    while i < len(t) and t[i] in _WS:
        i += 1
    return i


def _skip_string(t, i):
    k = i + 1
    while k < len(t) and t[k] != '"':
        k += 2 if t[k] == "\\" else 1
    return k + 1


def _skip_value(t, i):
    c = t[i]
    if c == '"':
        return _skip_string(t, i)
    if c in "{[":
        depth, k = 0, i
        while k < len(t):
            ch = t[k]
            if ch == '"':
                k = _skip_string(t, k)
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return k + 1
            k += 1
        return k
    k = i
    while k < len(t) and t[k] not in ",}]" and t[k] not in _WS:
        k += 1
    return k


def json_offset_of(text, path):
    """Смещение значения по пути в тексте JSON; None - пути нет или текст не разобрался."""
    i = _skip_ws(text, 0)
    for seg in path:
        opener = text[i] if i < len(text) else ""
        if opener == "{" and isinstance(seg, str):
            k, found = _skip_ws(text, i + 1), -1
            while k < len(text) and text[k] == '"':
                key_end = _skip_string(text, k)
                try:
                    name = json.loads(text[k:key_end])
                except ValueError:
                    return None
                value_start = _skip_ws(text, _skip_ws(text, key_end) + 1)
                if name == seg:
                    found = value_start
                    break
                k = _skip_ws(text, _skip_value(text, value_start))
                if k < len(text) and text[k] == ",":
                    k = _skip_ws(text, k + 1)
            if found < 0:
                return None
            i = found
        elif opener == "[" and isinstance(seg, int) and not isinstance(seg, bool):
            k, index, found = _skip_ws(text, i + 1), 0, -1
            while k < len(text) and text[k] != "]":
                if index == seg:
                    found = k
                    break
                k = _skip_ws(text, _skip_value(text, k))
                if k < len(text) and text[k] == ",":
                    k = _skip_ws(text, k + 1)
                index += 1
            if found < 0:
                return None
            i = found
        else:
            return None
    return i


def parse_error_text(error):
    if error.get("line"):
        return f"строка {error['line']}, столбец {error.get('column') or 1}: {error['message']}"
    return error["message"]


def editor_access(a):
    read = a["read"]
    read_only = bool(a["viewingVersion"] or not read.get("editable") or read.get("run_state") == "busy" or a["forbidden"])
    return {"readOnly": read_only, "canSave": not read_only and bool(a["dirty"]) and not a["saving"]}


def can_save_anyway(findings):
    """«Сохранить всё равно» возможно, если есть ошибки и среди них нет корневой и синтаксической."""
    errors = [f for f in findings if f["severity"] == "error"]
    return bool(errors) and all(f["code"] not in ("root", "syntax") for f in errors)


def error_count(findings):
    return sum(1 for f in findings if f["severity"] == "error")


def ancestor_pointers(pointer):
    """"/items/3/timing" -> ["/items", "/items/3"]: что раскрыть, чтобы увидеть поле."""
    parts = pointer.split("/")[1:]
    return ["/" + "/".join(parts[:i]) for i in range(1, len(parts))]


def summarize_validate_all(entries):
    errors = warnings = 0
    problems = []
    for e in entries:
        counts = e.get("counts")
        if counts:
            errors += counts["errors"]
            warnings += counts["warnings"]
        if e.get("error") or (counts and (counts["errors"] > 0 or counts["warnings"] > 0)):
            problems.append(e)
    return {"checked": len(entries), "errors": errors, "warnings": warnings,
            "passed": errors == 0 and all(not e.get("error") for e in entries), "problems": problems}


def group_of(path):
    """Группа поля для списка слева: первые два уровня пути, если второй - номер элемента массива."""
    if len(path) >= 2 and isinstance(path[1], int):
        return pointer_of(path[:2])
    return pointer_of(path[:1]) if len(path) > 1 else ""


def group_title(artifact_key, group_pointer):
    if not group_pointer:
        return "Общее"
    parts = [p.replace("~1", "/").replace("~0", "~") for p in group_pointer.split("/")[1:]]
    path = [int(p) if p.isdigit() and i > 0 else p for i, p in enumerate(parts)]
    head = label_for(artifact_key, path[:1])
    return f"{head} {path[1] + 1}" if len(path) == 2 else head


def findings_by_pointer(findings):
    out = {}
    for f in findings or []:
        out.setdefault(f.get("pointer", ""), []).append(f)
    return out


def parse_json_text(text):
    """Разбор текста JSON; ValueError с готовой фразой и номером строки."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(T["json_error"].format(reason=f"{exc.msg}, строка {exc.lineno}, столбец {exc.colno}")) from None
