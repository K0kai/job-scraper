"""Database connections for local SQLite and hosted PostgreSQL deployments."""
from __future__ import annotations

import os
import sqlite3
from typing import Any


def using_postgres() -> bool:
    return bool((os.environ.get("DATABASE_URL") or "").strip())


def _qmark_to_psycopg(query: str) -> str:
    """Translate SQLite-style placeholders without touching quoted SQL text."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(query):
        ch = query[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < len(query) and query[i + 1] == quote:
                    out.append(query[i + 1])
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class PostgresConnection:
    """Small compatibility wrapper for the app's existing ``db.execute`` calls."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, query: str, params: Any = None) -> Any:
        sql = _qmark_to_psycopg(query)
        if params is None:
            return self._connection.execute(sql)
        return self._connection.execute(sql, params)

    def executemany(self, query: str, params: Any) -> Any:
        return self._connection.cursor().executemany(_qmark_to_psycopg(query), params)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PostgresConnection":
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._connection.__exit__(exc_type, exc, tb)


def open_connection(path: str, *, timeout: float = 60.0) -> Any:
    """Use Neon/Postgres when DATABASE_URL is set; otherwise use local SQLite."""
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if database_url:
        import psycopg
        from psycopg.rows import dict_row

        connection = psycopg.connect(
            database_url,
            connect_timeout=max(1, min(60, int(timeout))),
            row_factory=dict_row,
        )
        return PostgresConnection(connection)

    db = sqlite3.connect(path, timeout=timeout)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA synchronous=NORMAL")
    return db
