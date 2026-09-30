"""Roteador de fluxo externo: decide handler, auto-submit × assistido × NO_HANDLER.

Substitui os checks hardcoded de ``is_inhire_url`` espalhados por
``linkedin_apply.py``/``app.py``. Os outcomes são strings de contrato — o
chamador (LinkedIn ou painel) decide o que fazer com cada um.

Registrar handler novo: criar ``ats_<site>.py`` com ``@register`` e importar na
lista ``_HANDLER_MODULES`` abaixo (1 linha; ver docs/ats-handler-guide.md).
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from ats_base import ApplyContext, BaseATSHandler, FillResult
from wait_human import wait_for_human

LOG = logging.getLogger("job-scraper")

OUTCOME_SUBMITTED = "submitted"
OUTCOME_ASSISTED = "assisted"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_FAILED = "failed"
OUTCOME_NO_HANDLER = "no_handler"

#: imports de efeito colateral (registro via @register) — um por site suportado
_HANDLER_MODULES: tuple[str, ...] = (
    "ats_inhire",
    "ats_greenhouse",
    "ats_lever",
    "ats_gupy",
)

_RELOAD_STATE = False


def _ensure_handlers_loaded() -> None:
    global _RELOAD_STATE
    if _RELOAD_STATE:
        return
    _RELOAD_STATE = True
    import importlib

    for module in _HANDLER_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:  # handler quebrado não derruba o app
            LOG.warning("falha ao registrar handler %s: %s", module, exc)


def find_handler(url: str):
    """Busca o handler da URL garantindo o lazy-load dos módulos registrados."""
    _ensure_handlers_loaded()
    from ats_base import find_handler as _find

    return _find(url)


def _host_of(url: str) -> str:
    try:
        return (urlparse(url or "").netloc or url or "")[:120]
    except Exception:
        return url or "?"


def _reanchor(page, context, handler_cls: type[BaseATSHandler]):
    """Popup pode abrir a candidatura em outra aba — re-âncora na que casa."""
    if context is None:
        return page
    for candidate in list(getattr(context, "pages", []) or []):
        try:
            if candidate is page:
                continue
            if handler_cls.can_handle(candidate.url or ""):
                try:
                    candidate.bring_to_front()
                except Exception:
                    pass
                return candidate
        except Exception:
            continue
    return page


def run_ats_flow(
    page,
    context,
    *,
    cfg: dict[str, str],
    rules: list[dict],
    resume_path: str,
    cover_letter: str,
    salary: str = "",
    human_wait: int = 15,
) -> tuple[str, str]:
    """Executa o fluxo de um ATS externo. Retorna ``(outcome, detail)``."""
    _ensure_handlers_loaded()
    try:
        current_url = page.url or ""
    except Exception:
        current_url = ""

    handler_cls = find_handler(current_url)
    if handler_cls is None:
        host = _host_of(current_url)
        detail = (
            f"NO_HANDLER: apply externo em {host} — nenhum driver ATS instalado. "
            "Preencha/envie manualmente se quiser."
        )
        return OUTCOME_NO_HANDLER, detail

    page = _reanchor(page, context, handler_cls)
    handler: type[BaseATSHandler] = handler_cls
    instance = handler_cls()
    ctx = ApplyContext(
        cfg=cfg,
        rules=rules,
        resume_path=resume_path,
        cover_letter=cover_letter,
        salary=salary,
    )

    try:
        if page.is_closed():
            return OUTCOME_FAILED, "pagina do ATS fechou antes do preenchimento"
    except Exception:
        pass

    if not instance.wait_ready(page):
        return OUTCOME_FAILED, "o formulario do ATS nao carregou a tempo"

    fill_res: FillResult = instance.fill(page, ctx)
    fill_note = ""
    if not fill_res.ok:
        bits = []
        if fill_res.missing:
            bits.append("faltou: " + ", ".join(fill_res.missing[:6]))
        if fill_res.error:
            bits.append(str(fill_res.error))
        fill_note = " (preenchimento incompleto — " + "; ".join(bits) + ")"

    obstacles = instance.detect_obstacles(page)
    if not obstacles and handler_cls.auto_submit_capable and fill_res.ok:
        submit_res = instance.submit(page)
        if submit_res.clicked:
            if _confirm_submission(instance, page):
                return OUTCOME_SUBMITTED, (
                    f"{handler_cls.name}: enviado automaticamente "
                    f"(botao: {submit_res.button_label})"
                )
            # submit clicado mas sem confirmacao — nao insiste, deixa com humano
            return _assisted_finish(instance, page, handler_cls, fill_note, human_wait)
        LOG.info("%s submit falhou (%s); caindo para assistido.", handler_cls.name, submit_res.error)

    return _assisted_finish(instance, page, handler_cls, fill_note, human_wait)


def _verify_now() -> float:
    return time.monotonic()


def _verify_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _confirm_submission(instance, page, *, timeout_s: float = 20) -> bool:
    deadline = _verify_now() + timeout_s
    while _verify_now() < deadline:
        try:
            if instance.verify_submitted(page) == "submitted":
                return True
        except Exception:
            return False
        _verify_sleep(2)
    return False


def _assisted_finish(instance, page, handler_cls, fill_note: str, human_wait: int) -> tuple[str, str]:
    LOG.info(
        "%s: preenchido. Resolva captcha/envio no Chrome (ate %s min). O robo nao envia sozinho.",
        handler_cls.name,
        human_wait,
    )
    outcome = wait_for_human(page, minutes=human_wait, success_regex=handler_cls.success_regex)
    if outcome == "submitted":
        return OUTCOME_ASSISTED, f"{handler_cls.name} preenchido; voce enviou (assistido){fill_note}."
    if outcome == "timeout":
        return OUTCOME_TIMEOUT, f"{handler_cls.name} preenchido; tempo esgotado sem confirmar envio{fill_note}."
    return OUTCOME_TIMEOUT, f"{handler_cls.name} preenchido; janela fechada sem confirmacao{fill_note}."


__all__ = [
    "OUTCOME_ASSISTED",
    "OUTCOME_FAILED",
    "OUTCOME_NO_HANDLER",
    "OUTCOME_SUBMITTED",
    "OUTCOME_TIMEOUT",
    "find_handler",
    "run_ats_flow",
]
