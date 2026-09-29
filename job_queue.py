# Fila persistente com workers paralelos e retry para operações de IA.
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from panel_log import log_event

LOG = logging.getLogger("job-scraper")

KIND_RESUME = "resume_analysis"
KIND_APPLY = "job_apply"
ACTIVE_STATUSES = ("pending", "running", "retry_wait")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def backoff_seconds(attempt: int) -> int:
    """Backoff exponencial com teto de 15 minutos."""
    base = 30 * (2 ** max(0, attempt - 1))
    return int(min(900, base))


def is_retryable_error(exc: BaseException) -> bool:
    name = exc.__class__.__name__.casefold()
    if "aiunavailable" in name:
        return True
    msg = str(exc).casefold()
    tokens = (
        "429", "503", "502", "504", "quota", "rate limit", "resource_exhausted",
        "unavailable", "overload", "temporar", "try again", "retry", "high demand",
        "ia indisponível", "resource exhausted",
    )
    return any(token in msg for token in tokens)


class JobQueue:
    """Fila SQLite + pool de workers (padrão 3) para jobs de IA."""

    def __init__(
        self,
        *,
        db_path: str,
        handlers: dict[str, Callable[[dict[str, Any]], str]],
        get_settings: Callable[[], dict[str, str]],
        connect_fn: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self.db_path = db_path
        self.handlers = handlers
        self.get_settings = get_settings
        self._connect_fn = connect_fn
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._dispatcher: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._inflight: dict[int, Future] = {}
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._connect_fn:
            return self._connect_fn()
        from dbutil import open_connection

        return open_connection(self.db_path, timeout=60.0)

    def _ensure_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS queue_jobs (
                  id INTEGER PRIMARY KEY,
                  kind TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL DEFAULT 40,
                  next_run_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  last_error TEXT NOT NULL DEFAULT '',
                  result TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS queue_jobs_status_idx ON queue_jobs(status, next_run_at)")
            db.execute("CREATE INDEX IF NOT EXISTS queue_jobs_kind_idx ON queue_jobs(kind)")

    def _limits(self) -> tuple[int, int, int]:
        cfg = self.get_settings()
        try:
            workers = max(1, min(8, int(cfg.get("queue_max_workers", "3") or 3)))
        except ValueError:
            workers = 3
        try:
            max_attempts = max(1, min(200, int(cfg.get("queue_max_attempts", "40") or 40)))
        except ValueError:
            max_attempts = 40
        try:
            ttl_hours = max(1, min(168, int(cfg.get("queue_ttl_hours", "24") or 24)))
        except ValueError:
            ttl_hours = 24
        return workers, max_attempts, ttl_hours

    def start(self) -> None:
        with self._lock:
            if self._dispatcher and self._dispatcher.is_alive():
                return
            workers, _, _ = self._limits()
            self._stop.clear()
            self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="queue-worker")
            self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True, name="queue-dispatcher")
            self._dispatcher.start()
            log_event("info", "queue", f"Fila iniciada com até {workers} worker(s).")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ex = self._executor
            self._executor = None
        if ex:
            ex.shutdown(wait=False, cancel_futures=False)
        log_event("info", "queue", "Fila parada.")

    def enqueue(self, kind: str, payload: dict[str, Any], *, dedupe_key: str | None = None) -> int:
        if kind not in self.handlers:
            raise ValueError(f"Tipo de job desconhecido: {kind}")
        _, max_attempts, ttl_hours = self._limits()
        now = _utc_now()
        payload_json = json.dumps(payload, ensure_ascii=False)
        if dedupe_key:
            payload = dict(payload)
            payload["_dedupe"] = dedupe_key
            payload_json = json.dumps(payload, ensure_ascii=False)
            with self._connect() as db:
                existing = db.execute(
                    """SELECT id FROM queue_jobs
                       WHERE kind=? AND status IN ('pending','running','retry_wait')
                         AND payload LIKE ?
                       ORDER BY id DESC LIMIT 1""",
                    (kind, f'%"{dedupe_key}"%'),
                ).fetchone()
                if existing:
                    log_event("info", "queue", f"Job #{existing['id']} ({kind}) já na fila; reutilizando.")
                    return int(existing["id"])
        with self._connect() as db:
            cur = db.execute(
                """INSERT INTO queue_jobs(
                     kind,payload,status,attempts,max_attempts,next_run_at,expires_at,last_error,result,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    kind,
                    payload_json,
                    "pending",
                    0,
                    max_attempts,
                    _iso(now),
                    _iso(now + timedelta(hours=ttl_hours)),
                    "",
                    "",
                    _iso(now),
                    _iso(now),
                ),
            )
            job_id = int(cur.lastrowid)
        log_event("info", "queue", f"Enfileirado #{job_id} ({kind}).")
        return job_id

    def list_jobs(self, *, limit: int = 100, include_terminal: bool = True) -> list[sqlite3.Row]:
        limit = max(1, min(300, limit))
        with self._connect() as db:
            if include_terminal:
                return list(db.execute("SELECT * FROM queue_jobs ORDER BY id DESC LIMIT ?", (limit,)))
            return list(
                db.execute(
                    "SELECT * FROM queue_jobs WHERE status IN ('pending','running','retry_wait') ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            )

    def cancel(self, job_id: int) -> bool:
        now = _iso(_utc_now())
        with self._connect() as db:
            cur = db.execute(
                """UPDATE queue_jobs SET status='cancelled', updated_at=?, last_error=COALESCE(NULLIF(last_error,''),'Cancelado manualmente')
                   WHERE id=? AND status IN ('pending','retry_wait','running')""",
                (now, job_id),
            )
            ok = cur.rowcount > 0
        if ok:
            log_event("warning", "queue", f"Job #{job_id} cancelado.")
        return ok

    def retry_now(self, job_id: int) -> bool:
        now = _iso(_utc_now())
        with self._connect() as db:
            cur = db.execute(
                """UPDATE queue_jobs SET status='pending', next_run_at=?, updated_at=?, last_error=''
                   WHERE id=? AND status IN ('retry_wait','failed','cancelled')""",
                (now, now, job_id),
            )
            ok = cur.rowcount > 0
        if ok:
            log_event("info", "queue", f"Job #{job_id} reenfileirado para execução imediata.")
        return ok

    def clear_terminal(self) -> int:
        with self._connect() as db:
            cur = db.execute(
                "DELETE FROM queue_jobs WHERE status IN ('succeeded','failed','cancelled')"
            )
            n = int(cur.rowcount or 0)
        log_event("info", "queue", f"Removidos {n} job(s) concluídos/falhos/cancelados.")
        return n

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status, COUNT(*) AS n FROM queue_jobs GROUP BY status").fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def _claim_batch(self, limit: int) -> list[sqlite3.Row]:
        now = _utc_now()
        now_s = _iso(now)
        claimed: list[sqlite3.Row] = []
        with self._connect() as db:
            # Expire old jobs first.
            db.execute(
                """UPDATE queue_jobs SET status='failed', last_error='Expirado (TTL da fila)', updated_at=?
                   WHERE status IN ('pending','retry_wait') AND expires_at < ?""",
                (now_s, now_s),
            )
            rows = db.execute(
                """SELECT * FROM queue_jobs
                   WHERE status IN ('pending','retry_wait') AND next_run_at <= ?
                   ORDER BY id ASC LIMIT ?""",
                (now_s, limit),
            ).fetchall()
            for row in rows:
                cur = db.execute(
                    """UPDATE queue_jobs SET status='running', updated_at=?
                       WHERE id=? AND status IN ('pending','retry_wait')""",
                    (now_s, row["id"]),
                )
                if cur.rowcount:
                    claimed.append(row)
        return claimed

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                workers, _, _ = self._limits()
                with self._lock:
                    # Drop finished futures
                    done = [jid for jid, fut in self._inflight.items() if fut.done()]
                    for jid in done:
                        self._inflight.pop(jid, None)
                    inflight = len(self._inflight)
                    capacity = max(0, workers - inflight)
                    executor = self._executor
                if capacity and executor is not None:
                    batch = self._claim_batch(capacity)
                    for row in batch:
                        job_id = int(row["id"])
                        fut = executor.submit(self._run_job, dict(row))
                        with self._lock:
                            self._inflight[job_id] = fut
            except Exception:
                LOG.exception("Falha no dispatcher da fila")
                log_event("error", "queue", "Falha no dispatcher da fila.")
            self._stop.wait(1.0)

    def _run_job(self, row: dict[str, Any]) -> None:
        job_id = int(row["id"])
        kind = str(row["kind"])
        attempts = int(row["attempts"]) + 1
        max_attempts = int(row["max_attempts"])
        now = _utc_now()
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        handler = self.handlers.get(kind)
        if not handler:
            self._finish(job_id, "failed", attempts, f"Handler ausente para {kind}", "")
            return
        log_event("info", "queue", f"Executando #{job_id} ({kind}), tentativa {attempts}/{max_attempts}.")
        try:
            result = handler(payload) or "ok"
            self._finish(job_id, "succeeded", attempts, "", str(result)[:1000])
            log_event("success", "queue", f"Job #{job_id} ({kind}) concluído.")
        except Exception as exc:
            err = f"{exc.__class__.__name__}: {exc}"
            expires = _parse_iso(str(row.get("expires_at") or ""))
            retryable = is_retryable_error(exc)
            if retryable and attempts < max_attempts and (expires is None or now < expires):
                delay = backoff_seconds(attempts)
                next_run = _iso(now + timedelta(seconds=delay))
                with self._connect() as db:
                    db.execute(
                        """UPDATE queue_jobs SET status='retry_wait', attempts=?, next_run_at=?, last_error=?, updated_at=?
                           WHERE id=? AND status='running'""",
                        (attempts, next_run, err[:1000], _iso(now), job_id),
                    )
                log_event(
                    "warning",
                    "queue",
                    f"Job #{job_id} ({kind}) em retry ({attempts}/{max_attempts}) em {delay}s: {err}",
                )
            else:
                self._finish(job_id, "failed", attempts, err[:1000], "")
                log_event("error", "queue", f"Job #{job_id} ({kind}) falhou em definitivo: {err}")

    def _finish(self, job_id: int, status: str, attempts: int, last_error: str, result: str) -> None:
        now = _iso(_utc_now())
        with self._connect() as db:
            db.execute(
                """UPDATE queue_jobs SET status=?, attempts=?, last_error=?, result=?, updated_at=?
                   WHERE id=?""",
                (status, attempts, last_error, result, now, job_id),
            )
