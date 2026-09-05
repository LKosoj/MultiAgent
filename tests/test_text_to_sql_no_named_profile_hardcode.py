"""W1-1.2b: контроль хардкода named-профиля ``muni_ru`` в общем коде.

Named-профили (column_aliases/significance/nlu_morphemes/...) выбираются
через env-переменные (``TEXT_TO_SQL_*_PROFILE``) либо, приоритетнее, через
DSN-профиль (см. ``dsn_profile.py`` и ``dsn_profile_overrides.py``). Если в
общем коде ``custom_tools/text_to_sql`` или ``workflow`` встречается
буквальный литерал ``"muni_ru"`` (или ``'muni_ru'``) — это признак того,
что конкретный муниципальный домен снова вшит гвоздями в код, который
должен оставаться профиль-агностичным (профиль подставляется конфигом,
а не кодом).

Именованный профиль ``muni_ru`` полностью удалён из репозитория (нет ни
секций ``profiles.muni_ru`` в yaml, ни обратной совместимости в коде) —
допустимых исключений больше нет. Любое повторное появление литерала в
коде — это регрессия (кто-то снова вшил конкретный домен гвоздями).

Тест ищет только буквальные строковые литералы (в кавычках), не трогая
докстринги/комментарии, где имя профиля упоминается как часть истории
изменений (это не хардкод, а документация).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Литерал ищем именно в кавычках (как строковую константу Python), чтобы не
# ловить упоминания в докстрингах/комментариях вида "например, muni_ru"
# или ``profiles.muni_ru``.
_LITERAL_RE = re.compile(r"""(["'])muni_ru\1""")


def _iter_scanned_files():
    text_to_sql_dir = REPO_ROOT / "custom_tools" / "text_to_sql"
    workflow_dir = REPO_ROOT / "workflow"
    yield from sorted(text_to_sql_dir.rglob("*.py"))
    if workflow_dir.is_dir():
        yield from sorted(workflow_dir.glob("*.py"))


def test_no_named_profile_hardcode_anywhere():
    violations = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _LITERAL_RE.search(line):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()!r}")

    assert not violations, (
        "Найден хардкод именованного профиля \"muni_ru\" — профиль удалён "
        "из репозитория, допустимых исключений больше нет. Имя профиля "
        "должно приходить из конфига/DSN-профиля, а не быть вшито в код "
        "(W1-1.2b):\n" + "\n".join(violations)
    )
