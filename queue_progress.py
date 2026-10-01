# Helper fino para progress_log da fila — evita import circular app↔handlers.
from __future__ import annotations

from typing import Any, Callable

ProgressFn = Callable[[str], None]


def noop_progress(_message: str) -> None:
    return None


def make_progress_fn(queue_obj: Any, queue_job_id: Any) -> ProgressFn:
    """Callback append-only; no-op se id inválido ou append falhar."""
    if queue_obj is None or queue_job_id is None:
        return noop_progress
    try:
        qid = int(queue_job_id)
    except (TypeError, ValueError):
        return noop_progress

    def progress(message: str) -> None:
        try:
            queue_obj.append_progress(qid, message)
        except Exception:
            pass

    return progress


def progress_from_ai(ai: dict | None) -> ProgressFn:
    fn = (ai or {}).get("progress") if isinstance(ai, dict) else None
    if callable(fn):
        return fn  # type: ignore[return-value]
    return noop_progress


__all__ = [
    "ProgressFn",
    "make_progress_fn",
    "noop_progress",
    "progress_from_ai",
]
