# Fila persistente com workers paralelos e retry para operações de IA.
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from panel_log import log_event
from dbutil import using_postgres

LOG = logging.getLogger("job-scraper")

KIND_RESUME = "resume_analysis"
KIND_APPLY = "job_apply"
KIND_WORTH_REVERIFY = "worth_reverify"
ACTIVE_STATUSES = ("pending", "running", "retry_wait")
# LinkedIn vacancy checks share one persistent Chrome profile.
BROWSER_PROFILE_KINDS = (KIND_WORTH_REVERIFY,)
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
# Teto absoluto do jitter de retry (evita intervalos de horas).
BACKOFF_CAP_SECONDS = 20 * 60


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


def backoff_seconds(attempt: int, *, rate_limited: bool = False) -> int:
    """Backoff exponencial com jitter aleatório por job (espalha retries sincronizados).

    Sem jitter, vários 429 voltam no mesmo segundo e martelam a IA de novo.
    Teto: 20 minutos.
    """
    floor = 60 if rate_limited else 20
    # attempt 1 → ~60–180s (429) ou ~20–60s; dobra a cada tentativa até o teto.
    base = floor * (2 ** max(0, attempt - 1))
    base = min(BACKOFF_CAP_SECONDS, base)
    low = floor
    high = min(BACKOFF_CAP_SECONDS, max(low + 15, int(base * 1.75)))
    return random.randint(low, high)


def is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).casefold()
    tokens = ("429", "rate limit", "quota", "resource_exhausted", "resource exhausted", "too many requests")
    return any(t in msg for t in tokens)


def is_retryable_error(exc: BaseException) -> bool:
    name = exc.__class__.__name__.casefold()
    if "nonretryable" in name:
        return False
    if "aiunavailable" in name:
        return True
    if "operationalerror" in name and "locked" in str(exc).casefold():
        return True
    msg = str(exc).casefold()
    # Never retry browser/login steps that require a person.
    linkedin_human = (
        "login", "checkpoint", "captcha", "authwall", "perfil chrome", "manual",
    )
    if any(token in msg for token in linkedin_human):
        return False
    tokens = (
        "429", "503", "502", "504", "quota", "rate limit", "resource_exhausted",
        "unavailable", "overload", "temporar", "try again", "retry", "high demand",
        "ia indisponível", "resource exhausted", "database is locked", "database locked",
    )
    return any(token in msg for token in tokens)


class NonRetryableError(Exception):
    """Queue should mark failed immediately (no retry_wait)."""



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
        # Neon is provisioned with the complete schema before the app starts.
        if using_postgres():
            return
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
            cols = {row["name"] for row in db.execute("PRAGMA table_info(queue_jobs)")}
            if "progress_log" not in cols:
                db.execute(
                    "ALTER TABLE queue_jobs ADD COLUMN progress_log TEXT NOT NULL DEFAULT ''"
                )

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
            recovered = self.recover_stale_running()
            respread = self.respread_retry_waits()
            self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="queue-worker")
            self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True, name="queue-dispatcher")
            self._dispatcher.start()
            msg = f"Fila iniciada com até {workers} worker(s)."
            if recovered:
                msg += f" {recovered} job(s) interrompido(s) recolocados como pendentes."
            if respread:
                msg += f" {respread} retry(s) reespalhado(s) com jitter."
            log_event("info", "queue", msg)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ex = self._executor
            self._executor = None
        if ex:
            ex.shutdown(wait=False, cancel_futures=False)
        log_event("info", "queue", "Fila parada.")

    def recover_stale_running(self) -> int:
        """Jobs 'running' sem processo vivo (ex.: após restart).

        Legacy browser-application jobs are cancelled; other interrupted work is requeued.
        """
        now = _utc_now()
        _, _, ttl_hours = self._limits()
        with self._connect() as db:
            cur_legacy = db.execute(
                """UPDATE queue_jobs
                   SET status='cancelled', updated_at=?,
                       last_error='Candidatura pelo navegador removida; use a extensão no Chrome para formulários.'
                   WHERE kind='linkedin_apply' AND status IN ('pending','running','retry_wait')""",
                (_iso(now),),
            )
            cancelled_legacy = int(cur_legacy.rowcount or 0)
            cur = db.execute(
                """UPDATE queue_jobs
                   SET status='pending', next_run_at=?, expires_at=?, updated_at=?,
                       last_error=CASE
                         WHEN last_error='' THEN 'Interrompido (reinício do app); reenfileirado.'
                         ELSE last_error
                       END
                   WHERE status='running'""",
                (_iso(now), _iso(now + timedelta(hours=ttl_hours)), _iso(now)),
            )
            return int(cur.rowcount or 0) + cancelled_legacy

    def respread_retry_waits(self) -> int:
        """Reespalha next_run_at de jobs em retry_wait para quebrar sincronização (ex.: 429 em massa)."""
        now = _utc_now()
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, attempts, last_error FROM queue_jobs WHERE status='retry_wait'"
            ).fetchall()
            n = 0
            for row in rows:
                rate_limited = is_rate_limit_error(Exception(row["last_error"] or ""))
                delay = backoff_seconds(max(1, int(row["attempts"] or 1)), rate_limited=rate_limited)
                db.execute(
                    "UPDATE queue_jobs SET next_run_at=?, updated_at=? WHERE id=? AND status='retry_wait'",
                    (_iso(now + timedelta(seconds=delay)), _iso(now), int(row["id"])),
                )
                n += 1
            return n

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
            row = db.execute(
                """INSERT INTO queue_jobs(
                     kind,payload,status,attempts,max_attempts,next_run_at,expires_at,last_error,result,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
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
            ).fetchone()
            job_id = int(row["id"])
        log_event("info", "queue", f"Enfileirado #{job_id} ({kind}).")
        return job_id

    def append_progress(self, job_id: int | None, message: str, *, max_chars: int = 12000) -> None:
        """Append-only na fila. Não apaga result/last_error. job_id None = no-op."""
        if job_id is None:
            return
        try:
            qid = int(job_id)
        except (TypeError, ValueError):
            return
        text = re.sub(r"\s+", " ", str(message or "").strip())
        if not text:
            return
        stamp = _utc_now().strftime("%H:%M:%S")
        line = f"{stamp} {text[:400]}"
        with self._connect() as db:
            row = db.execute(
                "SELECT progress_log FROM queue_jobs WHERE id=?", (qid,)
            ).fetchone()
            if not row:
                return
            prev = str(row["progress_log"] or "")
            merged = (prev + ("\n" if prev else "") + line).strip()
            if len(merged) > max_chars:
                merged = merged[-max_chars:]
                # evita cortar no meio da primeira linha visível
                nl = merged.find("\n")
                if nl > 0:
                    merged = merged[nl + 1 :]
            db.execute(
                "UPDATE queue_jobs SET progress_log=?, updated_at=? WHERE id=?",
                (merged, _iso(_utc_now()), qid),
            )

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

    def is_cancelled(self, job_id: int) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT status FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row) and str(row["status"]) == "cancelled"

    def retry_now(self, job_id: int) -> bool:
        now = _utc_now()
        _, max_attempts, ttl_hours = self._limits()
        with self._lock:
            fut = self._inflight.get(job_id)
            if fut is not None and not fut.done():
                return False
        with self._connect() as db:
            cur = db.execute(
                """UPDATE queue_jobs
                   SET status='pending', next_run_at=?, expires_at=?, updated_at=?,
                       last_error='', attempts=0, max_attempts=?, result=''
                   WHERE id=? AND kind!='linkedin_apply'
                     AND status IN ('retry_wait','failed','cancelled','running')""",
                (_iso(now), _iso(now + timedelta(hours=ttl_hours)), _iso(now), max_attempts, job_id),
            )
            ok = cur.rowcount > 0
        if ok:
            log_event("info", "queue", f"Job #{job_id} reenfileirado para execução imediata.")
        return ok

    def retry_all(self, *, statuses: tuple[str, ...] = ("failed", "cancelled", "retry_wait", "running")) -> int:
        """Reenfileira em lote jobs reprocessáveis (útil após falhas em massa / restart)."""
        now = _utc_now()
        _, max_attempts, ttl_hours = self._limits()
        with self._lock:
            live = {jid for jid, fut in self._inflight.items() if not fut.done()}
        placeholders = ",".join("?" * len(statuses))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id FROM queue_jobs WHERE kind!='linkedin_apply' AND status IN ({placeholders})",
                statuses,
            ).fetchall()
            ids = [int(row["id"]) for row in rows if int(row["id"]) not in live]
            if not ids:
                return 0
            id_ph = ",".join("?" * len(ids))
            cur = db.execute(
                f"""UPDATE queue_jobs
                    SET status='pending', next_run_at=?, expires_at=?, updated_at=?,
                        last_error='', attempts=0, max_attempts=?, result=''
                    WHERE id IN ({id_ph})""",
                [_iso(now), _iso(now + timedelta(hours=ttl_hours)), _iso(now), max_attempts, *ids],
            )
            n = int(cur.rowcount or 0)
        if n:
            log_event("info", "queue", f"{n} job(s) reenfileirado(s) em lote.")
        return n

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

    def _browser_profile_busy(self, db: sqlite3.Connection) -> bool:
        placeholders = ",".join("?" * len(BROWSER_PROFILE_KINDS))
        row = db.execute(
            f"SELECT 1 FROM queue_jobs WHERE kind IN ({placeholders}) AND status='running' LIMIT 1",
            BROWSER_PROFILE_KINDS,
        ).fetchone()
        return row is not None

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
            browser_busy = self._browser_profile_busy(db)
            # Fetch extra candidates so browser-profile jobs do not starve other work.
            fetch_n = max(limit * 4, limit + 8)
            rows = db.execute(
                """SELECT * FROM queue_jobs
                   WHERE status IN ('pending','retry_wait') AND next_run_at <= ?
                   ORDER BY id ASC LIMIT ?""",
                (now_s, fetch_n),
            ).fetchall()
            claimed_browser = False
            for row in rows:
                if len(claimed) >= limit:
                    break
                kind = str(row["kind"])
                if kind in BROWSER_PROFILE_KINDS and (browser_busy or claimed_browser):
                    continue
                cur = db.execute(
                    """UPDATE queue_jobs SET status='running', updated_at=?
                       WHERE id=? AND status IN ('pending','retry_wait')""",
                    (now_s, row["id"]),
                )
                if cur.rowcount:
                    claimed.append(row)
                    if kind in BROWSER_PROFILE_KINDS:
                        claimed_browser = True
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
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
        payload = dict(payload)
        payload["_queue_job_id"] = job_id
        # Se cancelaram entre claim e start, não começa.
        if self.is_cancelled(job_id):
            log_event("warning", "queue", f"Job #{job_id} ({kind}) já cancelado; não executa.")
            return
        handler = self.handlers.get(kind)
        if not handler:
            self._finish(job_id, "failed", attempts, f"Handler ausente para {kind}", "")
            return
        if attempts > 1:
            self.append_progress(job_id, f"— retry {attempts}/{max_attempts} —")
        else:
            self.append_progress(job_id, f"iniciando ({kind})")
        log_event("info", "queue", f"Executando #{job_id} ({kind}), tentativa {attempts}/{max_attempts}.")
        try:
            result = handler(payload) or "ok"
            self.append_progress(job_id, f"concluído: {str(result)[:200]}")
            self._finish(job_id, "succeeded", attempts, "", str(result)[:1000])
            log_event("success", "queue", f"Job #{job_id} ({kind}) concluído.")
        except Exception as exc:
            err = f"{exc.__class__.__name__}: {exc}"
            expires = _parse_iso(str(row.get("expires_at") or ""))
            retryable = is_retryable_error(exc)
            if self.is_cancelled(job_id):
                log_event("warning", "queue", f"Job #{job_id} ({kind}) cancelado durante execução.")
                return
            if retryable and attempts < max_attempts and (expires is None or now < expires):
                delay = backoff_seconds(attempts, rate_limited=is_rate_limit_error(exc))
                next_run = _iso(now + timedelta(seconds=delay))
                self.append_progress(job_id, f"retry em ~{delay}s: {err[:180]}")
                with self._connect() as db:
                    db.execute(
                        """UPDATE queue_jobs SET status='retry_wait', attempts=?, next_run_at=?, last_error=?, updated_at=?
                           WHERE id=? AND status='running'""",
                        (attempts, next_run, err[:1000], _iso(now), job_id),
                    )
                log_event(
                    "warning",
                    "queue",
                    f"Job #{job_id} ({kind}) em retry ({attempts}/{max_attempts}) em ~{delay}s: {err}",
                )
            else:
                self.append_progress(job_id, f"falhou: {err[:200]}")
                self._finish(job_id, "failed", attempts, err[:1000], "")
                log_event("error", "queue", f"Job #{job_id} ({kind}) falhou em definitivo: {err}")

    def _finish(self, job_id: int, status: str, attempts: int, last_error: str, result: str) -> None:
        now = _iso(_utc_now())
        with self._connect() as db:
            # Não sobrescreve cancelamento/manual mid-flight.
            db.execute(
                """UPDATE queue_jobs SET status=?, attempts=?, last_error=?, result=?, updated_at=?
                   WHERE id=? AND status='running'""",
                (status, attempts, last_error, result, now, job_id),
            )
