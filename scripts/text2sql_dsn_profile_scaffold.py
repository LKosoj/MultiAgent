#!/usr/bin/env python3
"""Scaffold a DSN-scoped Text-to-SQL profile.yaml for a target DSN (W1-1.2).

Интроспектирует схему тем же путём, что рантайм-код
(``SchemaLoader.get_database_schema``), и пишет
``sqlrag/<dsn_to_sanitized_name(dsn)>.profile.yaml`` с заполненными
identity-полями (``version``, ``dsn_fingerprint``, ``schema_namespace_version``,
``captured_at``) и пустыми доменными секциями (glossary/aliases/type_hints/
metric_hints/nlu_hints) — их заполняет оператор вручную под конкретную схему.

Использование:
    python3 scripts/text2sql_dsn_profile_scaffold.py --dsn <dsn> [--out PATH] [--force]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_tools.text_to_sql.dsn_profile import (  # noqa: E402
    DsnProfile,
    MetricHints,
    dsn_profile_path,
    dump_profile_yaml,
)
from custom_tools.text_to_sql.schema_cache import _dsn_host_port_db  # noqa: E402
from custom_tools.text_to_sql.schema_loader import SchemaLoader  # noqa: E402
from custom_tools.text_to_sql.schema_namespace import (  # noqa: E402
    canonical_schema_fingerprint,
)
from custom_tools.text_to_sql.utils import mask_dsn_value  # noqa: E402


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="DSN целевой базы данных")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Путь к файлу профиля (по умолчанию sqlrag/<sanitized>.profile.yaml)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Перезаписать существующий файл профиля"
    )
    return parser.parse_args(argv)


def _schema_hint_header(dsn: str, schema: Dict[str, Any]) -> str:
    lines = [
        "# DSN profile scaffold — заполните glossary/aliases/type_hints/",
        "# metric_hints/nlu_hints вручную под доменную специфику этой схемы.",
        f"# DSN (masked): {mask_dsn_value(dsn)}",
        "# Сгенерировано: scripts/text2sql_dsn_profile_scaffold.py",
        "#",
        "# Таблицы и колонки текущей схемы (справочно):",
    ]
    if not schema:
        lines.append("#   (схема пуста или не удалось интроспектировать таблицы)")
    for table_name in sorted(schema):
        table_schema = schema[table_name]
        columns = table_schema.get("columns", {}) if isinstance(table_schema, dict) else {}
        column_names = sorted(columns) if isinstance(columns, dict) else []
        lines.append(f"#   {table_name}: {', '.join(column_names)}")
    return "\n".join(lines) + "\n"


def build_scaffold_profile(*, dsn: str, schema: Dict[str, Any]) -> DsnProfile:
    return DsnProfile(
        version=1,
        dsn_fingerprint=_dsn_host_port_db(dsn),
        schema_namespace_version=canonical_schema_fingerprint(schema),
        captured_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        glossary=(),
        aliases={},
        type_hints={},
        metric_hints=MetricHints(),
        nlu_hints={},
        few_shots_ref=None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    out_path: Path = args.out if args.out is not None else dsn_profile_path(args.dsn)

    if out_path.exists() and not args.force:
        print(
            f"refuse to overwrite existing profile at {out_path} (use --force)",
            file=sys.stderr,
        )
        return 1

    loader = SchemaLoader(REPO_ROOT)
    # autosave=False (W1-1.2 blocker 2): скрипт пишет только явный --out
    # .profile.yaml, а не побочный sqlrag/<sanitized>.json.
    schema = loader.get_database_schema({}, dsn=args.dsn, autosave=False)

    profile = build_scaffold_profile(dsn=args.dsn, schema=schema)
    content = _schema_hint_header(args.dsn, schema) + "\n" + dump_profile_yaml(profile)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
