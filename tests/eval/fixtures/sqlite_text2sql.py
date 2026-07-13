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
            CREATE TABLE accounts (
                tenant_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                PRIMARY KEY (tenant_id, account_id)
            );
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                FOREIGN KEY (tenant_id, account_id)
                    REFERENCES accounts(tenant_id, account_id)
            );
            INSERT INTO accounts(tenant_id, account_id, name) VALUES
                (1, 10, 'alpha'),
                (1, 20, 'beta'),
                (2, 10, 'alpha');
            INSERT INTO invoices(id, tenant_id, account_id, amount) VALUES
                (1, 1, 10, 12.0),
                (2, 1, 10, 18.0),
                (3, 1, 20, 7.0),
                (4, 2, 10, 11.0);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path
