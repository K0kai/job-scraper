"""Perguntas do copiloto ao humano via painel (Chrome fica aberto)."""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable

LOG = logging.getLogger("job-scraper")

STATUS_PENDING = "pending"
STATUS_AWAITING_AI = "awaiting_ai"
STATUS_ANSWERED = "answered"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED_AI = "failed_ai"

PANEL_OPEN_STATUSES = (STATUS_PENDING, STATUS_AWAITING_AI)

ConnectFn = Callable[[], sqlite3.Connection]


def ensure_table(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS copilot_asks (
          id INTEGER PRIMARY KEY,
          job_id INTEGER,
          question TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          answer TEXT NOT NULL DEFAULT '',
          hint TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          answered_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    try:
        db.execute("ALTER TABLE copilot_asks ADD COLUMN hint TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass


def create_ask(
    connect_fn: ConnectFn,
    *,
    job_id: int | None,
    question: str,
    now_iso: str,
) -> int:
    q = (question or "").strip()
    if not q:
        raise ValueError("pergunta vazia")
    with connect_fn() as db:
        ensure_table(db)
        # Uma aberta por vez: expira pending e awaiting_ai anteriores.
        db.execute(
            f"UPDATE copilot_asks SET status=? WHERE status IN ({','.join('?' * len(PANEL_OPEN_STATUSES))})",
            (STATUS_EXPIRED, *PANEL_OPEN_STATUSES),
        )
        cur = db.execute(
            """INSERT INTO copilot_asks(job_id, question, status, answer, hint, created_at, answered_at)
               VALUES (?,?,?,?,?,?,?)""",
            (job_id, q, STATUS_PENDING, "", "", now_iso, ""),
        )
        return int(cur.lastrowid)


def get_pending_ask(connect_fn: ConnectFn) -> dict | None:
    """Ask visível no painel: pending (editável) ou awaiting_ai (spinner)."""
    with connect_fn() as db:
        ensure_table(db)
        row = db.execute(
            f"""SELECT id, job_id, question, status, answer, hint, created_at
               FROM copilot_asks
               WHERE status IN ({','.join('?' * len(PANEL_OPEN_STATUSES))})
               ORDER BY id DESC LIMIT 1""",
            PANEL_OPEN_STATUSES,
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "question": row["question"],
        "status": row["status"],
        "answer": row["answer"],
        "hint": row["hint"] or "",
        "created_at": row["created_at"],
    }


def get_ask(connect_fn: ConnectFn, ask_id: int) -> dict | None:
    with connect_fn() as db:
        ensure_table(db)
        row = db.execute(
            """SELECT id, job_id, question, status, answer, hint, created_at, answered_at
               FROM copilot_asks WHERE id=?""",
            (ask_id,),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def append_facts_both(connect_fn: ConnectFn, *, question: str, answer: str) -> None:
    """Grava a resposta em candidate_facts_pt e _en."""
    line = f"{(question or '').strip()}: {(answer or '').strip()}".strip(": ")
    if not line.strip(": "):
        return
    entry = line if ": " in line else f"Nota: {line}"
    with connect_fn() as db:
        for key in ("candidate_facts_pt", "candidate_facts_en"):
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            current = (row["value"] if row else "") or ""
            if entry in current:
                continue
            updated = (current.rstrip() + "\n" + entry).strip() if current.strip() else entry
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, updated),
            )


def answer_ask(
    connect_fn: ConnectFn,
    ask_id: int,
    answer: str,
    *,
    now_iso: str,
) -> bool:
    text = (answer or "").strip()
    if not text:
        return False
    with connect_fn() as db:
        ensure_table(db)
        row = db.execute(
            "SELECT id, question, status FROM copilot_asks WHERE id=?",
            (ask_id,),
        ).fetchone()
        if not row or row["status"] != STATUS_PENDING:
            return False
        db.execute(
            """UPDATE copilot_asks SET status=?, answer=?, hint=?, answered_at=? WHERE id=?""",
            (STATUS_AWAITING_AI, text, "", now_iso, ask_id),
        )
        question = row["question"]
    append_facts_both(connect_fn, question=question, answer=text)
    return True


def set_ask_hint(connect_fn: ConnectFn, ask_id: int, hint: str) -> None:
    with connect_fn() as db:
        ensure_table(db)
        db.execute(
            "UPDATE copilot_asks SET hint=? WHERE id=? AND status=?",
            ((hint or "").strip()[:240], ask_id, STATUS_AWAITING_AI),
        )


def complete_ask(connect_fn: ConnectFn, ask_id: int) -> bool:
    with connect_fn() as db:
        ensure_table(db)
        cur = db.execute(
            "UPDATE copilot_asks SET status=?, hint=? WHERE id=? AND status=?",
            (STATUS_ANSWERED, "", ask_id, STATUS_AWAITING_AI),
        )
        return cur.rowcount > 0


def fail_ask(connect_fn: ConnectFn, ask_id: int) -> bool:
    with connect_fn() as db:
        ensure_table(db)
        cur = db.execute(
            "UPDATE copilot_asks SET status=?, hint=? WHERE id=? AND status=?",
            (STATUS_FAILED_AI, "", ask_id, STATUS_AWAITING_AI),
        )
        return cur.rowcount > 0


def cancel_ask(connect_fn: ConnectFn, ask_id: int) -> bool:
    with connect_fn() as db:
        ensure_table(db)
        cur = db.execute(
            "UPDATE copilot_asks SET status=? WHERE id=? AND status=?",
            (STATUS_CANCELLED, ask_id, STATUS_PENDING),
        )
        return cur.rowcount > 0


def expire_ask(connect_fn: ConnectFn, ask_id: int) -> None:
    with connect_fn() as db:
        ensure_table(db)
        db.execute(
            "UPDATE copilot_asks SET status=? WHERE id=? AND status=?",
            (STATUS_EXPIRED, ask_id, STATUS_PENDING),
        )


def load_facts(connect_fn: ConnectFn, language: str) -> str:
    key = "candidate_facts_pt" if (language or "").casefold().startswith("pt") else "candidate_facts_en"
    with connect_fn() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return (row["value"] if row else "") or ""


def wait_for_answer(
    connect_fn: ConnectFn,
    ask_id: int,
    *,
    minutes: float = 12,
    poll_s: float = 1.5,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> tuple[str, str]:
    """Espera awaiting_ai/answered/cancelled/expired. Retorna (status, answer)."""
    deadline = monotonic_fn() + max(0.5, float(minutes)) * 60.0
    while monotonic_fn() < deadline:
        row = get_ask(connect_fn, ask_id)
        if not row:
            return STATUS_EXPIRED, ""
        st = row["status"]
        if st in {STATUS_AWAITING_AI, STATUS_ANSWERED}:
            return st, row.get("answer") or ""
        if st in {STATUS_CANCELLED, STATUS_EXPIRED, STATUS_FAILED_AI}:
            return st, ""
        sleep_fn(poll_s)
    expire_ask(connect_fn, ask_id)
    return STATUS_EXPIRED, ""


__all__ = [
    "STATUS_ANSWERED",
    "STATUS_AWAITING_AI",
    "STATUS_CANCELLED",
    "STATUS_EXPIRED",
    "STATUS_FAILED_AI",
    "STATUS_PENDING",
    "answer_ask",
    "append_facts_both",
    "cancel_ask",
    "complete_ask",
    "create_ask",
    "ensure_table",
    "expire_ask",
    "fail_ask",
    "get_ask",
    "get_pending_ask",
    "load_facts",
    "set_ask_hint",
    "wait_for_answer",
]
