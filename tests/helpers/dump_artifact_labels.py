"""Печатает таблицу подписей формы «Файлы» из схем studio/schemas (JSON для artifactLabels.ts).

Запуск: python3 tests/helpers/dump_artifact_labels.py
Вывод вставляют между маркерами // BEGIN JSON и // END JSON в
frontend/client/src/app/lib/storybook/artifactLabels.ts.
Ключ - шаблон JSON Pointer, где индексы массивов заменены на `*` (например `/items/*/timing`).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "custom_tools/storybook/studio/schemas"


def _walk(node, pointer: str, out: dict) -> None:
    if not isinstance(node, dict):
        return
    title = node.get("title")
    if isinstance(title, str) and title:
        out[pointer] = title
    for name, sub in (node.get("properties") or {}).items():
        _walk(sub, f"{pointer}/{name.replace('~', '~0').replace('/', '~1')}", out)
    if isinstance(node.get("items"), dict):
        _walk(node["items"], f"{pointer}/*", out)


def derive_labels() -> dict:
    labels = {}
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        out: dict = {}
        _walk(json.loads(path.read_text(encoding="utf-8")), "", out)
        labels[path.name[: -len(".schema.json")]] = out
    return labels


if __name__ == "__main__":
    sys.stdout.write(json.dumps(derive_labels(), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
