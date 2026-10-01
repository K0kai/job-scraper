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
from typing import Any
from urllib.parse import urlparse

from ats_base import ApplyContext, BaseATSHandler, FillResult
from wait_human import wait_for_human

LOG = logging.getLogger("job-scraper")

OUTCOME_SUBMITTED = "submitted"
OUTCOME_ASSISTED = "assisted"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_FAILED = "failed"
OUTCOME_NO_HANDLER = "no_handler"
#: copiloto de IA assumiu, não conseguiu, fechou a página — nota AMARELA no painel
OUTCOME_UNAUTOMATED = "unautomated"

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


def copilot_rescue(page, context, cfg: dict, ai: dict, *, reason: str) -> tuple[str, str, object]:
    """Chama o copiloto de IA (override total). Nunca levanta exceção — se o
    módulo falhar, cai para UNAVAILABLE e o fluxo segue o comportamento antigo."""
    try:
        from ats_copilot import ABORTED, SOLVED, SUBMITTED, copilot_takeover
        from queue_progress import progress_from_ai

        progress_from_ai(ai)(f"copiloto: {reason[:160]}")
        state, detail, active = copilot_takeover(page, context, reason=reason, cfg=cfg, ai=ai)
        if state == SUBMITTED:
            progress_from_ai(ai)(f"copiloto enviou: {str(detail)[:140]}")
            return "submitted", detail, active
        if state == SOLVED:
            progress_from_ai(ai)(f"copiloto ok: {str(detail)[:140]}")
            return "solved", detail, active
        if state == ABORTED:
            progress_from_ai(ai)(f"copiloto abortou: {str(detail)[:160]}")
            # Página ainda aberta → revisão humana; None → fechou de verdade.
            return "aborted", detail, active
        return "unavailable", detail, page
    except Exception as exc:
        LOG.warning("copiloto falhou (%s); seguindo sem ele.", exc)
        return "unavailable", str(exc), page


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
    ai: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Executa o fluxo de um ATS externo. Retorna ``(outcome, detail)``.

    ``ai``: contexto p/ perguntas de empresa via IA (job, resume_summary,
    resume_json, facts, provider, model, api_key, connect_fn, now_iso).
    """
    _ensure_handlers_loaded()
    try:
        current_url = page.url or ""
    except Exception:
        current_url = ""

    ai = dict(ai or {})
    if resume_path and not ai.get("resume_path"):
        ai["resume_path"] = resume_path

    from apply_mode import apply_mode_is_auto
    from queue_progress import progress_from_ai

    progress = progress_from_ai(ai)
    want_auto = apply_mode_is_auto(cfg)

    handler_cls = find_handler(current_url)
    if handler_cls is None:
        host = _host_of(current_url)
        progress(f"sem driver ATS ({host}) — copiloto")
        # Site sem driver: em modo auto o copiloto pode Submit; em review só preenche.
        ai_finish = dict(ai)
        ai_finish["allow_submit"] = want_auto
        if resume_path:
            ai_finish["resume_path"] = resume_path
        state, note, cpage = copilot_rescue(
            page,
            context,
            cfg,
            ai_finish,
            reason=(
                f"pagina de candidatura sem driver ATS instalado ({host}) — "
                + ("complete e envie" if want_auto else "preencha; humano revisa/envia")
            ),
        )
        if state == "submitted":
            try:
                if cpage is not None and not cpage.is_closed():
                    cpage.close()
            except Exception:
                pass
            return OUTCOME_SUBMITTED, note or f"copiloto enviou candidatura em {host}"
        if state == "solved" and cpage is not None:
            # Fallback assistido se a IA destravou sem confirmar envio.
            outcome = wait_for_human(cpage, minutes=human_wait)
            if outcome == "submitted":
                return OUTCOME_ASSISTED, f"copiloto destravou {host}; voce enviou (assistido)."
            return OUTCOME_TIMEOUT, f"copiloto destravou {host}; sem confirmacao de envio."
        if state == "aborted":
            handoff = cpage if cpage is not None else page
            try:
                still_open = handoff is not None and not handoff.is_closed()
            except Exception:
                still_open = False
            if still_open:
                progress(f"copiloto sem automação em {host} — aguardando você")
                outcome = wait_for_human(handoff, minutes=human_wait)
                if outcome == "submitted":
                    return OUTCOME_ASSISTED, f"copiloto abortou em {host}; voce enviou (assistido)."
                return OUTCOME_TIMEOUT, f"copiloto abortou em {host}; sem confirmacao de envio."
            return OUTCOME_UNAUTOMATED, note
        detail = (
            f"NO_HANDLER: apply externo em {host} — nenhum driver ATS instalado. "
            "Preencha/envie manualmente se quiser."
        )
        return OUTCOME_NO_HANDLER, detail

    page = _reanchor(page, context, handler_cls)
    handler: type[BaseATSHandler] = handler_cls
    instance = handler_cls()
    progress(f"ATS {handler_cls.name} — preenchendo")
    ctx = ApplyContext(
        cfg=cfg,
        rules=rules,
        resume_path=resume_path,
        cover_letter=cover_letter,
        salary=salary,
        ai=ai or {},
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
        # Travou: o copiloto assume com override total antes de chamar o humano.
        progress(f"ATS travou — copiloto ({fill_note.strip(' ()')[:140]})")
        ai_stuck = dict(ai or {})
        ai_stuck["allow_submit"] = want_auto
        state, note, cpage = copilot_rescue(page, context, cfg, ai_stuck,
                                           reason="formulario travado — " + fill_note.strip(" ()"))
        if state == "aborted":
            handoff = cpage if cpage is not None else page
            try:
                still_open = handoff is not None and not handoff.is_closed()
            except Exception:
                still_open = False
            if still_open:
                progress("copiloto sem automação — aguardando você no Chrome")
                return _assisted_finish(
                    instance, handoff, handler_cls, fill_note + f" ({note})", human_wait, ctx
                )
            return OUTCOME_UNAUTOMATED, note
        if state == "submitted":
            progress("copiloto enviou após travamento")
            return OUTCOME_SUBMITTED, note or f"{handler_cls.name}: copiloto enviou apos travamento"
        if state == "solved" and cpage is not None:
            # IA destravou (clicou Next, abriu o proximo passo). NAO re-executamos
            # instance.fill — em sites que ja avancaram (ex.: InHire) isso daria
            # duplo-avanco. Assumimos o passo liberado e seguimos a politica normal.
            progress("copiloto destravou — seguindo fluxo")
            page = cpage
            fill_res = FillResult(ok=True, filled=list(fill_res.filled), missing=[])
            fill_note = " (copiloto destravou)"

    obstacles = instance.detect_obstacles(page)
    # Modo auto: tenta enviar mesmo se o handler ainda não declarou auto_submit_capable,
    # desde que não haja captcha/login. Modo review: nunca auto-envia.
    may_auto_submit = bool(
        want_auto and fill_res.ok and not obstacles
        and (handler_cls.auto_submit_capable or want_auto)
    )
    if may_auto_submit:
        progress("modo auto — tentando enviar")
        submit_res = instance.submit(page)
        if submit_res.clicked:
            if _confirm_submission(instance, page):
                progress("envio confirmado (auto)")
                return OUTCOME_SUBMITTED, (
                    f"{handler_cls.name}: enviado automaticamente "
                    f"(botao: {submit_res.button_label})"
                )
            # submit clicado mas sem confirmacao — nao insiste, deixa com humano
            progress("Submit sem confirmação — aguardando você")
            return _assisted_finish(instance, page, handler_cls, fill_note, human_wait, ctx)
        LOG.info("%s submit falhou (%s); caindo para assistido.", handler_cls.name, submit_res.error)

    progress("preenchido — aguardando você no Chrome")
    return _assisted_finish(instance, page, handler_cls, fill_note, human_wait, ctx)


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


def _assisted_finish(instance, page, handler_cls, fill_note: str, human_wait: int, ctx: ApplyContext | None = None) -> tuple[str, str]:
    # Hybrid captcha (pydoll): se e Turnstile, tentamos o clique humanizado ANTES
    # de incomodar voce. reCAPTCHA/hCaptcha/puzzles continuam com o humano.
    try:
        from hybrid_captcha import try_solve_turnstile

        if try_solve_turnstile(page):
            LOG.info("%s: turnstile resolvido por hybrid automation.", handler_cls.name)
    except Exception as exc:
        LOG.debug("hybrid turnstile: %s", exc)
    LOG.info(
        "%s: preenchido. Resolva captcha/envio no Chrome (ate %s min). O robo nao envia sozinho.",
        handler_cls.name,
        human_wait,
    )
    on_tick = None
    if ctx is not None:
        def on_tick(p):  # watcher do handler durante a espera (ex.: modal de perguntas)
            instance.watch_wait(p, ctx)
    outcome = wait_for_human(page, minutes=human_wait, success_regex=handler_cls.success_regex, on_tick=on_tick)
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
    "OUTCOME_UNAUTOMATED",
    "copilot_rescue",
    "find_handler",
    "run_ats_flow",
]
