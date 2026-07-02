"""SQLite fixtures for deterministic Text-to-SQL eval tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def create_sqlite_text2sql_fixture(path: str | Path) -> Path:
    path = Path(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                region TEXT NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );
            INSERT INTO customers(id, region) VALUES
                (1, 'north'),
                (2, 'south');
            INSERT INTO orders(id, customer_id, amount) VALUES
                (1, 1, 100.0),
                (2, 1, 25.0),
                (3, 2, 50.0);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path
