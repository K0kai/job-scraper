"""Espera humana genérica em fluxo assistido (qualquer ATS, não só InHire)."""
from __future__ import annotations

import logging
import re
import time

LOG = logging.getLogger("job-scraper")

DEFAULT_SUCCESS_RE = re.compile(
    r"candidatura\s+enviada|application\s+(has\s+been\s+)?(sent|received|submitted)|"
    r"obrigad[oa]\s+por\s+(sua\s+)?(candidatura|inscri[cç][aã]o|application)|"
    r"recebemos\s+sua\s+candidatura|inscri[cç][aã]o\s+enviada|"
    r"thank\s+you.{0,60}(for\s+)?(your\s+)?(application|applying)|"
    r"envio\s+confirmado|we('|\u2019)?ve\s+received\s+your\s+application",
    re.I,
)


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def page_shows_success(page, success_regex: re.Pattern[str] | None = None) -> bool:
    """True se o body atual casa com a regex de sucesso (estrita)."""
    regex = success_regex or DEFAULT_SUCCESS_RE
    try:
        snip = (page.inner_text("body") or "")[:5000]
    except Exception:
        return False
    return bool(regex.search(snip))


def wait_for_success_signal(
    page,
    *,
    timeout_s: float = 20,
    success_regex: re.Pattern[str] | None = None,
    poll_s: float = 1.5,
) -> bool:
    """Espera texto de sucesso real após um clique de envio. Sem match → False."""
    regex = success_regex or DEFAULT_SUCCESS_RE
    deadline = _now() + max(1.0, float(timeout_s))
    while _now() < deadline:
        if page_shows_success(page, regex):
            return True
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        _sleep(poll_s)
    return False


def wait_for_human(page, *, minutes: int, success_regex: re.Pattern[str] | None = None, on_tick=None) -> str:
    """'submitted' | 'abandoned' | 'timeout' — o humano resolve captcha/envio.

    ``on_tick(page)`` roda a cada checagem (opcional): permite ao handler ATS
    vigiar/interagir com widgets que aparecem DEPOIS do captcha (ex.: modal de
    perguntas da empresa na InHire). Excecoes do callback nunca quebram a espera.
    """
    regex = success_regex or DEFAULT_SUCCESS_RE
    deadline = _now() + max(1, minutes) * 60
    while _now() < deadline:
        _sleep(4)
        if on_tick is not None:
            try:
                on_tick(page)
            except Exception as exc:
                LOG.debug("on_tick falhou (seguindo a espera): %s", exc)
        try:
            if page_shows_success(page, regex):
                return "submitted"
        except Exception:
            return "abandoned"
        try:
            if page.is_closed():
                return "abandoned"
        except Exception:
            return "abandoned"
    return "timeout"


def wait_for_manual_handoff(
    page,
    *,
    max_minutes: int = 180,
    success_regex: re.Pattern[str] | None = None,
    poll_s: float = 5.0,
) -> str:
    """Modo manual: IA/bot não interferem. Chrome fica aberto até sucesso, o humano
    fechar a janela, ou esgotar ``max_minutes`` (só então o caller pode liberar o lock).

    Retorna 'submitted' | 'abandoned' | 'timeout'. Sem on_tick — zero automação.
    """
    regex = success_regex or DEFAULT_SUCCESS_RE
    deadline = _now() + max(1, int(max_minutes)) * 60
    while _now() < deadline:
        _sleep(max(1.0, float(poll_s)))
        try:
            if page.is_closed():
                return "abandoned"
        except Exception:
            return "abandoned"
        try:
            if page_shows_success(page, regex):
                return "submitted"
        except Exception:
            return "abandoned"
    return "timeout"
