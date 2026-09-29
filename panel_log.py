# Log persistente para o painel local (visível na aba Logs).
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "jobs.db")
MAX_ROWS = 800
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    from dbutil import open_connection

    return open_connection(DB_PATH, timeout=60.0)


def ensure_log_table(db: sqlite3.Connection | None = None) -> None:
    own = db is None
    if own:
        db = _connect()
    assert db is not None
    db.execute(
        """CREATE TABLE IF NOT EXISTS event_logs (
          id INTEGER PRIMARY KEY,
          created_at TEXT NOT NULL,
          level TEXT NOT NULL,
          source TEXT NOT NULL,
          message TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS event_logs_created_idx ON event_logs(created_at DESC)")
    if own:
        db.commit()
        db.close()


def log_event(level: str, source: str, message: str) -> None:
    """Registra um evento para a aba Logs. Níveis: info, success, warning, error."""
    level = (level or "info").casefold().strip()
    if level not in {"info", "success", "warning", "error", "debug"}:
        level = "info"
    source = (source or "app")[:80]
    message = (message or "").strip()[:2000]
    if not message:
        return
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        try:
            with _connect() as db:
                ensure_log_table(db)
                db.execute(
                    "INSERT INTO event_logs(created_at,level,source,message) VALUES(?,?,?,?)",
                    (stamp, level, source, message),
                )
                db.execute(
                    """DELETE FROM event_logs WHERE id NOT IN (
                         SELECT id FROM event_logs ORDER BY id DESC LIMIT ?
                       )""",
                    (MAX_ROWS,),
                )
        except Exception:
            pass


def list_logs(limit: int = 200) -> list[sqlite3.Row]:
    limit = max(1, min(500, int(limit)))
    with _connect() as db:
        ensure_log_table(db)
        return list(db.execute("SELECT * FROM event_logs ORDER BY id DESC LIMIT ?", (limit,)))


def clear_logs() -> int:
    with _lock:
        with _connect() as db:
            ensure_log_table(db)
            cur = db.execute("DELETE FROM event_logs")
            return int(cur.rowcount or 0)


class PanelLogHandler(logging.Handler):
    """Encaminha records do logging stdlib para a aba Logs."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level_name = record.levelname.casefold()
            if level_name == "warning":
                level = "warning"
            elif level_name in {"error", "critical"}:
                level = "error"
            elif level_name == "debug":
                level = "debug"
            else:
                level = "info"
            log_event(level, record.name or "logger", record.getMessage())
        except Exception:
            self.handleError(record)


def attach_to_logger(logger_name: str = "job-scraper") -> None:
    logger = logging.getLogger(logger_name)
    for existing in list(logger.handlers):
        if isinstance(existing, PanelLogHandler):
            return
    handler = PanelLogHandler()
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
