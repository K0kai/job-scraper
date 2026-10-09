"""Browser-free liveness checks for jobs in the review list."""
from __future__ import annotations

import logging
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger("odradek-scraper")
ConnectFn = Callable[[], Any]
AbortFn = Callable[[], bool]

INACTIVE = "inactive"
ACTIVE = "active"
UNKNOWN = "unknown"

INACTIVE_RE = re.compile(
    r"no\s+longer\s+accept\w*\s+(?:applications?|applicants?|candidates?)|"
    r"no\s+longer\s+open|this\s+job\s+is\s+no\s+longer\s+available|"
    r"job\s+has\s+been\s+filled|position\s+has\s+been\s+filled|"
    r"(?:is\s+)?not\s+(?:currently\s+)?accepting\s+(?:applications?|applicants?|candidates?)|"
    r"n[aã]o\s+aceita(?:mos)?\s+(?:mais\s+)?candidaturas|closed-job(?:__flavor)?|"
    r"esta\s+vaga\s+n[aã]o\s+est[aá]\s+mais\s+aceitando|"
    r"esta\s+vaga\s+n[aã]o\s+est[aá]\s+mais\s+dispon[ií]vel|"
    r"vaga\s+(?:encerrada|expirada|indispon[ií]vel)|"
    r"n[aã]o\s+est(?:[aá]|amos)\s+mais\s+aceitando\s+candidaturas|"
    r"n[aã]o\s+estamos\s+mais\s+recebendo\s+candidaturas|"
    r"\bcandidaturas?\s+encerradas?\b|inscri[cç][oõ]es?\s+encerradas?|"
    r"application\s+deadline\s+has\s+passed|this\s+position\s+is\s+closed",
    re.I,
)
ACTIVE_RE = re.compile(
    r"apply\s+now|candidatar(-se)?|enviar\s+candidatura|"
    r"submit\s+application|apply\s+for\s+this\s+(job|role)|finalizar\s+candidatura",
    re.I,
)
LINKEDIN_JOB_ID_RE = re.compile(
    r"linkedin\.com/(?:[\w.-]+/)?jobs/view/(?:[\w%-]*?-)?(\d+)", re.I
)
GUEST_APPLY_MARKERS = (
    "public_jobs_apply-link-onsite",
    "public_jobs_apply-link-offsite",
    "job-details-topcard-apply-modal",
    "topcard-apply",
)
USER_AGENT = "JobScraper/1.0 (public job availability check)"


def text_indicates_closed(text: str) -> bool:
    return bool(INACTIVE_RE.search(text or ""))


def apify_item_indicates_closed(item: dict) -> bool:
    """Detect closed-job flags in public Apify actor results during collection."""
    if not isinstance(item, dict):
        return False
    for key in (
        "closed", "isClosed", "jobClosed", "applicationsClosed", "isJobClosed",
        "expired", "isExpired",
    ):
        val = item.get(key)
        if val is True or str(val).strip().casefold() in {"1", "true", "yes"}:
            return True
    state = str(
        item.get("jobState") or item.get("jobStatus") or item.get("listingStatus") or ""
    ).strip().casefold()
    if state in {"closed", "expired", "inactive", "filled", "archived"}:
        return True
    blob = " ".join(
        str(item.get(k) or "")
        for k in (
            "title", "position", "jobTitle", "displayTitle", "descriptionText",
            "descriptionHtml", "description", "jobDescription", "jobDescriptionHTML",
            "closedJobText", "jobStateMessage",
        )
    )
    return text_indicates_closed(blob)


def extract_linkedin_job_id(url: str) -> str | None:
    raw = (url or "").strip()
    match = LINKEDIN_JOB_ID_RE.search(raw)
    if match:
        return match.group(1)
    try:
        query = parse_qs(urlparse(raw).query or "")
        for key in ("currentJobId", "jobId", "trkJobId"):
            values = query.get(key) or []
            if values and str(values[0]).isdigit():
                return str(values[0])
    except Exception:
        pass
    return None


def fetch_url_text(url: str, *, timeout: float = 12.0) -> tuple[int, str]:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read(300_000)
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                body = raw.decode(charset, errors="replace")
            except LookupError:
                body = raw.decode("utf-8", errors="replace")
            return int(response.status), body
    except HTTPError as exc:
        raw = exc.read(100_000) if hasattr(exc, "read") else b""
        return int(exc.code), raw.decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError):
        return 0, ""
    except Exception:
        return 0, ""


def classify_http(status: int, body: str) -> str:
    if status in {404, 410, 451}:
        return INACTIVE
    if status >= 500 or status in {0, 401, 403}:
        return UNKNOWN
    if INACTIVE_RE.search(body or ""):
        return INACTIVE
    if ACTIVE_RE.search(body or ""):
        return ACTIVE
    return UNKNOWN


def check_linkedin_guest_liveness(job_id: str, *, timeout: float = 12.0) -> tuple[str, str]:
    """Use LinkedIn's publicly accessible guest job-posting endpoint, without login/browser."""
    jid = str(job_id or "").strip()
    if not jid.isdigit():
        return UNKNOWN, "ID público da vaga indisponível"
    status, body = fetch_url_text(
        f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}", timeout=timeout
    )
    if status == 0:
        return UNKNOWN, "endpoint público indisponível"
    verdict = classify_http(status, body)
    if verdict == INACTIVE:
        return INACTIVE, f"endpoint público HTTP {status} indica vaga encerrada"
    if any(marker in body for marker in GUEST_APPLY_MARKERS):
        return ACTIVE, "endpoint público mostra candidatura disponível"
    if verdict == ACTIVE:
        return ACTIVE, "endpoint público mostra sinal de candidatura"
    return UNKNOWN, f"endpoint público HTTP {status} sem sinal conclusivo"


def check_job_liveness(job: dict, *, http_timeout: float = 12.0) -> tuple[str, str]:
    url = (job.get("url") or "").strip()
    if not url:
        return UNKNOWN, "sem URL"
    if "linkedin.com" in (urlparse(url).netloc or "").casefold():
        job_id = extract_linkedin_job_id(url)
        if job_id:
            return check_linkedin_guest_liveness(job_id, timeout=http_timeout)
    status, body = fetch_url_text(url, timeout=http_timeout)
    if status == 0:
        return UNKNOWN, "falha de rede ou timeout"
    verdict = classify_http(status, body)
    return verdict, f"HTTP {status}" + (" sem sinal conclusivo" if verdict == UNKNOWN else "")


def reverify_worth_jobs(
    connect_fn: ConnectFn,
    *,
    should_abort: AbortFn | None = None,
    http_timeout: float = 12.0,
) -> dict[str, int | bool]:
    """Check all worth jobs over public HTTP; only clear evidence can mark inactive."""
    with connect_fn() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, url, title, company, source FROM jobs WHERE status='worth' ORDER BY id ASC"
        ).fetchall()]
    summary: dict[str, int | bool] = {
        "checked": 0, "ignored": 0, "active": 0, "unknown": 0, "aborted": False,
    }
    for job in rows:
        if should_abort and should_abort():
            summary["aborted"] = True
            break
        try:
            verdict, detail = check_job_liveness(job, http_timeout=http_timeout)
        except Exception as exc:
            verdict, detail = UNKNOWN, str(exc)
        summary["checked"] = int(summary["checked"]) + 1
        if verdict == INACTIVE:
            with connect_fn() as db:
                db.execute(
                    "UPDATE jobs SET status='ignored', notes=? WHERE id=? AND status='worth'",
                    (f"Ignorada na verificação diária (inativa): {detail}"[:500], int(job["id"])),
                )
            summary["ignored"] = int(summary["ignored"]) + 1
            LOG.info("worth-check: vaga #%s marcada inativa (%s)", job["id"], detail)
        elif verdict == ACTIVE:
            summary["active"] = int(summary["active"]) + 1
        else:
            summary["unknown"] = int(summary["unknown"]) + 1
            LOG.info("worth-check: vaga #%s mantida inconclusiva (%s)", job["id"], detail)
    return summary


def format_summary(summary: dict[str, int | bool]) -> str:
    result = (
        f"Verificação diária: {summary.get('checked', 0)} checadas, "
        f"{summary.get('ignored', 0)} inativas removidas, "
        f"{summary.get('active', 0)} ativas, "
        f"{summary.get('unknown', 0)} inconclusivas mantidas."
    )
    return result + (" Interrompida por cancelamento." if summary.get("aborted") else "")
