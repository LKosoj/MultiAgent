#!/usr/bin/env python3
"""Seed unreviewed Text-to-SQL eval candidates from logs/sql_history.jsonl."""

from __future__ import annotations

import argparse

from custom_tools.text_to_sql.constants import SQL_HISTORY_FILE
from custom_tools.text_to_sql.eval.history_import import seed_candidates_from_history


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", default=str(SQL_HISTORY_FILE))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    count = seed_candidates_from_history(args.history, args.output)
    print(f"Wrote {count} unreviewed candidate cases to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
