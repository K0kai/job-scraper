"""Espera humana genérica em fluxo assistido (qualquer ATS, não só InHire)."""
from __future__ import annotations

import logging
import re
import time

LOG = logging.getLogger("job-scraper")

DEFAULT_SUCCESS_RE = re.compile(
    r"candidatura\s+enviada|application\s+(sent|received)|obrigad[oa]|recebemos\s+sua|"
    r"inscri[cç][aã]o\s+enviada|thank\s+you.*application|envio\s+confirmado",
    re.I,
)


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


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
            snip = (page.inner_text("body") or "")[:5000]
        except Exception:
            return "abandoned"
        if regex.search(snip):
            return "submitted"
        try:
            if page.is_closed():
                return "abandoned"
        except Exception:
            return "abandoned"
    return "timeout"
