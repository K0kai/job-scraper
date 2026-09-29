"""Conexões SQLite com WAL e busy_timeout para melhor concorrência."""
from __future__ import annotations

import sqlite3


def open_connection(path: str, *, timeout: float = 60.0) -> sqlite3.Connection:
    """Abre o banco com journal WAL e espera ocupação antes de falhar."""
    db = sqlite3.connect(path, timeout=timeout)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA synchronous=NORMAL")
    return db
