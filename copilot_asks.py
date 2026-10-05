"""Perguntas do copiloto ao humano via painel (Chrome fica aberto)."""
from __future__ import annotations

import logging
import os
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

KIND_TEXT = "text"
KIND_FILE = "file"

# Modal só fica aberto enquanto o humano ainda não respondeu.
# `awaiting_ai` era usado para segurar o spinner durante a próxima chamada à IA
# (rate-limit podia segurar por vários minutos) — não entra mais no painel.
PANEL_OPEN_STATUSES = (STATUS_PENDING,)

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
          kind TEXT NOT NULL DEFAULT 'text',
          file_path TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          answered_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    for ddl in (
        "ALTER TABLE copilot_asks ADD COLUMN hint TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE copilot_asks ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'",
        "ALTER TABLE copilot_asks ADD COLUMN file_path TEXT NOT NULL DEFAULT ''",
    ):
        try:
            db.execute(ddl)
        except sqlite3.OperationalError:
            pass


def create_ask(
    connect_fn: ConnectFn,
    *,
    job_id: int | None,
    question: str,
    now_iso: str,
    kind: str = KIND_TEXT,
) -> int:
    q = (question or "").strip()
    if not q:
        raise ValueError("pergunta vazia")
    ask_kind = KIND_FILE if str(kind or "").casefold() == KIND_FILE else KIND_TEXT
    with connect_fn() as db:
        ensure_table(db)
        db.execute(
            f"UPDATE copilot_asks SET status=? WHERE status IN ({','.join('?' * len(PANEL_OPEN_STATUSES))})",
            (STATUS_EXPIRED, *PANEL_OPEN_STATUSES),
        )
        cur = db.execute(
            """INSERT INTO copilot_asks(job_id, question, status, answer, hint, kind, file_path, created_at, answered_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (job_id, q, STATUS_PENDING, "", "", ask_kind, "", now_iso, ""),
        )
        return int(cur.lastrowid)


def get_pending_ask(connect_fn: ConnectFn) -> dict | None:
    """Ask visível no painel: só pending (ainda sem resposta do humano)."""
    with connect_fn() as db:
        ensure_table(db)
        # Asks antigos em awaiting_ai (spinner eterno) → fecha na leitura.
        db.execute(
            "UPDATE copilot_asks SET status=?, hint=? WHERE status=?",
            (STATUS_ANSWERED, "", STATUS_AWAITING_AI),
        )
        row = db.execute(
            f"""SELECT id, job_id, question, status, answer, hint, kind, file_path, created_at
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
        "kind": row["kind"] or KIND_TEXT,
        "file_path": row["file_path"] or "",
        "needs_file": (row["kind"] or KIND_TEXT) == KIND_FILE,
        "created_at": row["created_at"],
    }


def get_ask(connect_fn: ConnectFn, ask_id: int) -> dict | None:
    with connect_fn() as db:
        ensure_table(db)
        row = db.execute(
            """SELECT id, job_id, question, status, answer, hint, kind, file_path, created_at, answered_at
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
    file_path: str = "",
) -> bool:
    text = (answer or "").strip()
    path = (file_path or "").strip()
    if not text and not path:
        return False
    if not text and path:
        text = f"arquivo: {os.path.basename(path)}"
    with connect_fn() as db:
        ensure_table(db)
        row = db.execute(
            "SELECT id, question, status, kind FROM copilot_asks WHERE id=?",
            (ask_id,),
        ).fetchone()
        if not row or row["status"] != STATUS_PENDING:
            return False
        if (row["kind"] or KIND_TEXT) == KIND_FILE and not path:
            return False
        db.execute(
            """UPDATE copilot_asks SET status=?, answer=?, hint=?, file_path=?, answered_at=? WHERE id=?""",
            (STATUS_ANSWERED, text, "", path, now_iso, ask_id),
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


def save_ask_upload(
    *,
    uploads_dir: str,
    ask_id: int,
    filename: str,
    raw_bytes: bytes,
) -> str:
    """Persiste upload do modal; devolve caminho absoluto."""
    if not raw_bytes:
        raise ValueError("arquivo vazio")
    if len(raw_bytes) > 12 * 1024 * 1024:
        raise ValueError("arquivo maior que 12 MB")
    os.makedirs(uploads_dir, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (filename or "upload.bin"))[:80]
    stored = os.path.join(uploads_dir, f"ask_{ask_id}_{int(time.time())}_{safe}")
    with open(stored, "wb") as handle:
        handle.write(raw_bytes)
    return stored


__all__ = [
    "KIND_FILE",
    "KIND_TEXT",
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
    "save_ask_upload",
    "set_ask_hint",
    "wait_for_answer",
]
