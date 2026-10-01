"""Reverifica vagas em 'worth': HTTP primeiro; browser para LinkedIn/inconclusivo."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger("job-scraper")

ConnectFn = Callable[[], Any]
AbortFn = Callable[[], bool]

INACTIVE = "inactive"
ACTIVE = "active"
UNKNOWN = "unknown"

INACTIVE_RE = re.compile(
    r"no\s+longer\s+accepting\s+applications|"
    r"this\s+job\s+is\s+no\s+longer\s+available|"
    r"job\s+has\s+been\s+filled|"
    r"position\s+has\s+been\s+filled|"
    r"esta\s+vaga\s+n[aã]o\s+est[aá]\s+mais\s+aceitando|"
    r"vaga\s+(encerrada|expirada|indispon[ií]vel)|"
    r"n[aã]o\s+est[aá]\s+mais\s+aceitando\s+candidaturas|"
    r" candidaturas?\s+encerradas?|"
    r"application\s+deadline\s+has\s+passed|"
    r"this\s+position\s+is\s+closed",
    re.I,
)

ACTIVE_RE = re.compile(
    r"easy\s*apply|candidatura\s*simplificada|"
    r"apply\s+now|candidatar(-se)?|enviar\s+candidatura|"
    r"submit\s+application|apply\s+for\s+this\s+(job|role)|"
    r"finalizar\s+candidatura",
    re.I,
)

USER_AGENT = "JobScraperLocal/0.1 (personal job search; liveness check)"
DEFAULT_PAGE_WAIT_MS = 25_000
DEFAULT_POLL_MS = 500


def needs_browser_check(url: str) -> bool:
    host = (urlparse(url or "").netloc or "").casefold()
    return "linkedin.com" in host


def classify_http(status: int, body: str) -> str:
    if status in {404, 410, 451}:
        return INACTIVE
    if status >= 500 or status in {401, 403}:
        return UNKNOWN
    text = body or ""
    if INACTIVE_RE.search(text):
        return INACTIVE
    if ACTIVE_RE.search(text):
        return ACTIVE
    if status != 200:
        return UNKNOWN
    return UNKNOWN


def fetch_url_text(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
    req = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(400_000)
            charset = "utf-8"
            ctype = (resp.headers.get_content_charset() or "").strip()
            if ctype:
                charset = ctype
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return int(resp.status), text
    except HTTPError as exc:
        raw = exc.read(200_000) if hasattr(exc, "read") else b""
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return int(exc.code), text
    except URLError:
        return 0, ""
    except Exception:
        return 0, ""


def check_http_liveness(url: str, *, timeout: float = 20.0) -> tuple[str, str]:
    if not (url or "").strip():
        return UNKNOWN, "URL vazia"
    status, body = fetch_url_text(url, timeout=timeout)
    if status == 0:
        return UNKNOWN, "falha de rede"
    verdict = classify_http(status, body)
    return verdict, f"HTTP {status}"


def wait_page_liveness_signal(
    page,
    *,
    timeout_ms: int = DEFAULT_PAGE_WAIT_MS,
    poll_ms: int = DEFAULT_POLL_MS,
    should_abort: AbortFn | None = None,
) -> tuple[str, str]:
    """Espera sinal claro (ativa/inativa) no HTML; senão timeout → unknown."""
    deadline = time.monotonic() + max(0.05, timeout_ms / 1000.0)
    last_detail = "sem leitura"
    while True:
        if should_abort and should_abort():
            return UNKNOWN, "abortado"
        final_url = getattr(page, "url", "") or ""
        host_path = final_url.casefold()
        if any(tok in host_path for tok in ("/login", "authwall", "/checkpoint", "/challenge")):
            return UNKNOWN, "login/authwall LinkedIn"
        body = ""
        try:
            body = page.content() or ""
        except Exception as exc:
            last_detail = f"content: {exc}"
            body = ""
        verdict = classify_http(200, body)
        if verdict == INACTIVE:
            return INACTIVE, "browser: vaga fechada"
        if verdict == ACTIVE:
            return ACTIVE, "browser: candidatura disponível"
        last_detail = "HTML ainda sem sinal claro"
        if time.monotonic() >= deadline:
            return UNKNOWN, f"timeout esperando sinal ({last_detail})"
        try:
            page.wait_for_timeout(max(1, int(poll_ms)))
        except Exception:
            time.sleep(max(0.001, poll_ms / 1000.0))


class LinkedInLivenessSession:
    """Uma sessão Chrome para várias URLs LinkedIn (não abre/fecha por vaga)."""

    def __init__(
        self,
        *,
        cfg: dict[str, str] | None = None,
        project_root: str = "",
        page_timeout_ms: int = DEFAULT_PAGE_WAIT_MS,
        goto_timeout_ms: int = 60_000,
        should_abort: AbortFn | None = None,
    ) -> None:
        self.cfg = cfg or {}
        self.project_root = project_root or "."
        self.page_timeout_ms = page_timeout_ms
        self.goto_timeout_ms = goto_timeout_ms
        self.should_abort = should_abort
        self._playwright = None
        self._context = None
        self._page = None
        self._lock_held = False

    def __enter__(self) -> "LinkedInLivenessSession":
        from browser_engine import (
            BACKGROUND_RUN_ARGS,
            persistent_launch_kwargs,
            resolve_sync_playwright,
        )
        from linkedin_apply import _LINKEDIN_LOCK, _LOCK_WAIT_SECONDS, default_profile_dir

        profile = (self.cfg.get("linkedin_chrome_profile") or "").strip() or default_profile_dir(
            self.project_root
        )
        acquired = _LINKEDIN_LOCK.acquire(timeout=min(120, _LOCK_WAIT_SECONDS))
        if not acquired:
            raise RuntimeError("perfil Chrome ocupado (Easy Apply em andamento)")
        self._lock_held = True
        try:
            module = resolve_sync_playwright(self.cfg)
            sync_playwright = module.sync_playwright
            self._playwright = sync_playwright().start()
            launch_kwargs = persistent_launch_kwargs(
                profile,
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                args=list(BACKGROUND_RUN_ARGS),
            )
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    channel="chrome", **launch_kwargs
                )
            except Exception:
                self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            LOG.info("worth-reverify: sessão Chrome aberta para lote LinkedIn")
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        self._context = None
        self._page = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        if self._lock_held:
            try:
                from linkedin_apply import _LINKEDIN_LOCK

                _LINKEDIN_LOCK.release()
            except Exception:
                pass
            self._lock_held = False
        LOG.info("worth-reverify: sessão Chrome fechada")
        return False

    def check_url(self, url: str) -> tuple[str, str]:
        if self._page is None:
            return UNKNOWN, "sessão Chrome ausente"
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.goto_timeout_ms)
        except Exception as exc:
            return UNKNOWN, f"goto: {exc}"
        # Pausa inicial para o shell SPA começar a hidratar.
        try:
            self._page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2.0)
        try:
            self._page.mouse.wheel(0, 400)
        except Exception:
            pass
        return wait_page_liveness_signal(
            self._page,
            timeout_ms=self.page_timeout_ms,
            poll_ms=DEFAULT_POLL_MS,
            should_abort=self.should_abort,
        )


def check_job_liveness(
    job: dict,
    *,
    cfg: dict[str, str] | None = None,
    project_root: str = "",
    http_timeout: float = 20.0,
    browser_session: LinkedInLivenessSession | None = None,
) -> tuple[str, str]:
    url = (job.get("url") or "").strip()
    if not url:
        return UNKNOWN, "sem URL"

    if needs_browser_check(url):
        if browser_session is not None:
            return browser_session.check_url(url)
        # Fallback isolado (testes / chamada avulsa): abre sessão só para esta URL.
        try:
            with LinkedInLivenessSession(cfg=cfg, project_root=project_root) as session:
                return session.check_url(url)
        except Exception as exc:
            return UNKNOWN, f"browser: {exc}"

    http_verdict, http_detail = check_http_liveness(url, timeout=http_timeout)
    return http_verdict, http_detail


def reverify_worth_jobs(
    connect_fn: ConnectFn,
    *,
    cfg: dict[str, str] | None = None,
    project_root: str = "",
    should_abort: AbortFn | None = None,
) -> dict[str, int | bool]:
    """Percorre todas as vagas status=worth; ignora só inactive claro.

    LinkedIn: uma sessão Chrome para o lote inteiro. Respeita should_abort entre vagas.
    """
    with connect_fn() as db:
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT id, url, title, company, source, status FROM jobs WHERE status='worth' ORDER BY id ASC"
            ).fetchall()
        ]

    summary: dict[str, int | bool] = {
        "checked": 0,
        "ignored": 0,
        "active": 0,
        "unknown": 0,
        "aborted": False,
    }
    linkedin_jobs = [j for j in rows if needs_browser_check(j.get("url") or "")]
    other_jobs = [j for j in rows if not needs_browser_check(j.get("url") or "")]

    def _apply(job: dict, verdict: str, detail: str) -> None:
        summary["checked"] = int(summary["checked"]) + 1
        if verdict == INACTIVE:
            with connect_fn() as db:
                db.execute(
                    "UPDATE jobs SET status='ignored', notes=? WHERE id=? AND status='worth'",
                    (
                        f"Ignorada na reverificação (inativa): {detail}"[:500],
                        int(job["id"]),
                    ),
                )
            summary["ignored"] = int(summary["ignored"]) + 1
            LOG.info("worth-reverify: job #%s ignored (%s)", job["id"], detail)
        elif verdict == ACTIVE:
            summary["active"] = int(summary["active"]) + 1
        else:
            summary["unknown"] = int(summary["unknown"]) + 1
            LOG.info("worth-reverify: job #%s kept (%s / %s)", job["id"], verdict, detail)

    def _aborted() -> bool:
        return bool(should_abort and should_abort())

    for job in other_jobs:
        if _aborted():
            summary["aborted"] = True
            LOG.warning("worth-reverify: abortado (cancel) após %s checadas", summary["checked"])
            return summary
        try:
            verdict, detail = check_job_liveness(job, cfg=cfg, project_root=project_root)
        except Exception as exc:
            verdict, detail = UNKNOWN, str(exc)
        _apply(job, verdict, detail)

    if not linkedin_jobs:
        return summary
    if _aborted():
        summary["aborted"] = True
        return summary

    try:
        with LinkedInLivenessSession(
            cfg=cfg, project_root=project_root, should_abort=should_abort
        ) as session:
            for job in linkedin_jobs:
                if _aborted():
                    summary["aborted"] = True
                    LOG.warning(
                        "worth-reverify: abortado (cancel) no meio do lote LinkedIn "
                        "após %s checadas",
                        summary["checked"],
                    )
                    break
                try:
                    verdict, detail = check_job_liveness(
                        job,
                        cfg=cfg,
                        project_root=project_root,
                        browser_session=session,
                    )
                except Exception as exc:
                    verdict, detail = UNKNOWN, str(exc)
                _apply(job, verdict, detail)
    except Exception as exc:
        LOG.warning("worth-reverify: sessão LinkedIn falhou (%s)", exc)
        # Se a sessão nem abriu, as LI ainda não foram checadas — marca unknown.
        already = int(summary["checked"]) - len(other_jobs)
        if already < 0:
            already = 0
        for job in linkedin_jobs[already:]:
            if _aborted():
                summary["aborted"] = True
                break
            _apply(job, UNKNOWN, f"browser: {exc}")

    return summary


def format_summary(summary: dict[str, int | bool]) -> str:
    base = (
        f"Reverificação: {summary.get('checked', 0)} checadas, "
        f"{summary.get('ignored', 0)} ignoradas (inativas), "
        f"{summary.get('active', 0)} ativas, "
        f"{summary.get('unknown', 0)} inconclusivas mantidas."
    )
    if summary.get("aborted"):
        return base + " Interrompida por cancelamento."
    return base
