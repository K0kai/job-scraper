"""Reverifica vagas em 'worth': HTTP primeiro; browser para LinkedIn/inconclusivo."""
from __future__ import annotations

import logging
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger("job-scraper")

ConnectFn = Callable[[], Any]

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


def check_browser_liveness(
    url: str,
    *,
    cfg: dict[str, str] | None = None,
    project_root: str = "",
    timeout_ms: int = 25_000,
) -> tuple[str, str]:
    """Abre a URL no perfil Chrome (LinkedIn) e classifica pelo HTML visível."""
    cfg = cfg or {}
    try:
        from browser_engine import persistent_launch_kwargs, resolve_sync_playwright
        from linkedin_apply import _LINKEDIN_LOCK, _LOCK_WAIT_SECONDS, default_profile_dir
    except ImportError as exc:
        return UNKNOWN, f"browser indisponível: {exc}"

    profile = (cfg.get("linkedin_chrome_profile") or "").strip() or default_profile_dir(project_root or ".")
    acquired = _LINKEDIN_LOCK.acquire(timeout=min(120, _LOCK_WAIT_SECONDS))
    if not acquired:
        return UNKNOWN, "perfil Chrome ocupado (Easy Apply em andamento)"

    try:
        module = resolve_sync_playwright(cfg)
        sync_playwright = module.sync_playwright
        with sync_playwright() as p:
            launch_kwargs = persistent_launch_kwargs(
                profile,
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            try:
                context = p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            except Exception:
                context = p.chromium.launch_persistent_context(**launch_kwargs)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1200)
                final_url = page.url or url
                body = ""
                try:
                    body = page.content() or ""
                except Exception:
                    body = ""
                host_path = (final_url or "").casefold()
                if any(tok in host_path for tok in ("/login", "authwall", "/checkpoint", "/challenge")):
                    return UNKNOWN, "login/authwall LinkedIn"
                verdict = classify_http(200, body)
                if verdict == UNKNOWN and needs_browser_check(url):
                    return UNKNOWN, "HTML LinkedIn inconclusivo"
                return verdict, "browser"
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as exc:
        return UNKNOWN, f"browser: {exc}"
    finally:
        _LINKEDIN_LOCK.release()


def check_job_liveness(
    job: dict,
    *,
    cfg: dict[str, str] | None = None,
    project_root: str = "",
    http_timeout: float = 20.0,
) -> tuple[str, str]:
    url = (job.get("url") or "").strip()
    if not url:
        return UNKNOWN, "sem URL"

    http_verdict, http_detail = check_http_liveness(url, timeout=http_timeout)
    use_browser = needs_browser_check(url) or http_verdict == UNKNOWN
    if not use_browser:
        return http_verdict, http_detail

    # LinkedIn ou HTTP inconclusivo: tentar browser (só se LinkedIn ou host exige).
    if needs_browser_check(url):
        return check_browser_liveness(url, cfg=cfg, project_root=project_root)

    # Host não-LinkedIn com HTTP unknown — não abrir browser genérico (custo/ruído).
    return http_verdict, http_detail


def reverify_worth_jobs(
    connect_fn: ConnectFn,
    *,
    cfg: dict[str, str] | None = None,
    project_root: str = "",
) -> dict[str, int]:
    """Percorre todas as vagas status=worth; ignora só inactive claro."""
    with connect_fn() as db:
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT id, url, title, company, source, status FROM jobs WHERE status='worth' ORDER BY id ASC"
            ).fetchall()
        ]

    summary = {"checked": 0, "ignored": 0, "active": 0, "unknown": 0}
    for job in rows:
        summary["checked"] += 1
        try:
            verdict, detail = check_job_liveness(job, cfg=cfg, project_root=project_root)
        except Exception as exc:
            verdict, detail = UNKNOWN, str(exc)
        if verdict == INACTIVE:
            with connect_fn() as db:
                db.execute(
                    "UPDATE jobs SET status='ignored', notes=? WHERE id=? AND status='worth'",
                    (
                        f"Ignorada na reverificação (inativa): {detail}"[:500],
                        int(job["id"]),
                    ),
                )
            summary["ignored"] += 1
            LOG.info("worth-reverify: job #%s ignored (%s)", job["id"], detail)
        elif verdict == ACTIVE:
            summary["active"] += 1
        else:
            summary["unknown"] += 1
            LOG.info("worth-reverify: job #%s kept (%s / %s)", job["id"], verdict, detail)
    return summary


def format_summary(summary: dict[str, int]) -> str:
    return (
        f"Reverificação: {summary.get('checked', 0)} checadas, "
        f"{summary.get('ignored', 0)} ignoradas (inativas), "
        f"{summary.get('active', 0)} ativas, "
        f"{summary.get('unknown', 0)} inconclusivas mantidas."
    )
