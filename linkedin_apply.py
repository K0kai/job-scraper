# LinkedIn Easy Apply — assisted only (fill + you submit).
# Never auto-clicks Submit. Hard daily/gap caps. Checkpoint → wait for human.
# Still may violate LinkedIn ToS; lower ban surface than mass auto-submit bots.
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from form_rules import find_rule_for_label, job_context_text, list_rules, pick_select_option, prepare_text_value, resolve_rule_value
from resume_pipeline import AiUnavailableError

from apply_channels import (
    ConnectFn,
    _insert_application,
    generate_open_answer,
    lookslike_open_question,
)
from ats_router import find_handler

LOG = logging.getLogger("job-scraper")

# One Chrome profile / one Easy Apply wizard at a time across queue workers.
_LINKEDIN_LOCK = threading.Lock()
_LOCK_WAIT_SECONDS = 20 * 60

EASY_APPLY_RE = re.compile(r"easy\s*apply|candidatura\s*simplificada", re.I)
EXTERNAL_APPLY_RE = re.compile(
    r"apply\s*on\s*company\s*site|candidatar[- ]se\s+no\s+site|aplicar\s+no\s+site|"
    r"apply\s*on\s*company|company\s*website",
    re.I,
)
EXTERNAL_APPLY_BTN_RE = re.compile(
    r"^(apply|candidatar(-se)?|aplicar)(\s|$)|apply\s+on\s+company",
    re.I,
)
NEXT_RE = re.compile(r"^(next|continue|próximo|continuar|avançar)$", re.I)
REVIEW_RE = re.compile(r"review|revisar|rever", re.I)
SUBMIT_RE = re.compile(r"submit\s*application|enviar\s*candidatura|submit|enviar", re.I)
ALREADY_RE = re.compile(r"applied|candidatou|já\s+se\s+candidatou", re.I)
SUCCESS_RE = re.compile(
    r"application\s+sent|candidatura\s+enviada|your\s+application\s+was\s+sent|"
    r"aplicação\s+enviada|submitted",
    re.I,
)
# Hints de logout só em SEGMENTOS de path — nunca substring no URL inteiro
# (bug: "challenge_id=77" em tracking param era lido como checkpoint/login).
LOGGED_OUT_PATH_TOKENS = ("login", "authwall", "uas", "checkpoint", "challenge", "session_redirect")
CHECKPOINT_PATH_TOKENS = ("checkpoint", "challenge", "captcha")
# Widgets reais de captcha — evidência estrutural única na página.
CAPTCHA_EVIDENCE_MARKERS = (
    "g-recaptcha",
    "recaptcha/api.js",
    "recaptcha.net",
    "h-captcha",
    "hcaptcha.com",
    "challenges.cloudflare.com/turnstile",
    "cf-turnstile",
)
# Frase de verificação precisa vir JUNTO de um formulário de verificação
# (evidência estrutural dupla); sozinha em copy de vaga é falso positivo.
VERIFY_PHRASE_RE = re.compile(
    r"unusual\s+activity|verify\s+it.?s\s+you|security\s+verification|"
    r"verifica(r|ção)\s+(de\s+)?(sua\s+)?(identidade|conta|seguran[aç]a)",
    re.I,
)


def _path_tokens(url: str) -> list[str]:
    try:
        parts = urlparse(url or "").path.casefold().split("/")
    except Exception:
        return []
    return [p for p in parts if p]


def _url_has_path_token(url: str, tokens: tuple[str, ...]) -> bool:
    return any(tok in _path_tokens(url) for tok in tokens)

# Conservative defaults — override via settings, never raise above hard ceilings.
# Easy Apply: only min-gap pacing (no daily cap). Floor/ceiling clamp the setting.
HARD_MIN_GAP_MINUTES = 1
HARD_MAX_GAP_MINUTES = 30
DEFAULT_MIN_GAP_MINUTES = 3
DEFAULT_HUMAN_WAIT_MINUTES = 12
DEFAULT_LOGIN_WAIT_MINUTES = 25


def default_profile_dir(root: str) -> str:
    return os.path.join(root, "linkedin_browser_profile")


def _int_setting(cfg: dict[str, str], key: str, default: int, *, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int((cfg.get(key) or str(default)).strip() or default)))
    except ValueError:
        return default


def _human_pause(lo: float = 0.35, hi: float = 1.1) -> None:
    time.sleep(random.uniform(lo, hi))


def _human_type(locator, text: str) -> None:
    """Type like a person: clear, then per-character with jitter (no instant fill)."""
    locator.click(timeout=4000)
    _human_pause(0.15, 0.4)
    # Select-all + delete is less bot-like than .fill on LinkedIn fields.
    locator.press("Control+a")
    _human_pause(0.05, 0.15)
    locator.press("Backspace")
    _human_pause(0.1, 0.3)
    for ch in text:
        locator.type(ch, delay=random.randint(35, 120))
        if random.random() < 0.04:
            time.sleep(random.uniform(0.2, 0.55))


def _normalize_job_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return raw
    match = re.search(r"(?:jobs/view/|currentJobId=)(\d+)", raw)
    if match:
        return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
    return raw


def _page_text_snip(page, limit: int = 6000) -> str:
    try:
        return (page.inner_text("body") or "")[:limit]
    except Exception:
        try:
            return (page.content() or "")[:limit]
        except Exception:
            return ""


def _page_looks_logged_out(page) -> bool:
    """Prefer false positives (wait for human) over proceeding on a guest/authwall page."""
    try:
        href = (page.url or "").casefold()
    except Exception:
        href = ""
    if _url_has_path_token(href, LOGGED_OUT_PATH_TOKENS):
        return True

    try:
        if page.locator('input[name="session_key"], input#username, form.login__form').count():
            return True
    except Exception:
        pass

    # Logged-in chrome: global nav present and no login form.
    try:
        if page.locator(".global-nav__me, img.global-nav__me-photo").count():
            return False
    except Exception:
        pass

    body = _page_text_snip(page, 8000).casefold()
    if _page_has_checkpoint(page) and "sign in" not in body and "entrar" not in body:
        return False

    guest_signals = (
        "sign in to view more jobs",
        "sign in to see who",
        "join now",
        "novo no linkedin",
        "cadastre-se",
        "agree & join",
        "welcome back",
    )
    if any(sig in body for sig in guest_signals):
        return True
    if "sign in" in body and "join now" in body:
        return True
    if "entrar" in body and ("cadastre-se" in body or "join now" in body):
        return True
    # Guest job view often has Sign in / Entrar and no member Easy Apply.
    if "easy apply" not in body and "candidatura simplificada" not in body:
        if "sign in" in body or ("entrar" in body and "linkedin" in body):
            return True
    return False


def _page_has_checkpoint(page) -> bool:
    """Só com evidência estrutural: rota de verificação OU widget de captcha.

    Antes bastava a palavra 'challenge'/'verificação' no texto (descrições de
    vaga comuns) e a página normal travava esperando um captcha inexistente.
    """
    try:
        href = page.url or ""
    except Exception:
        href = ""
    if _url_has_path_token(href, CHECKPOINT_PATH_TOKENS):
        return True
    try:
        html = (page.content() or "")[:400000].casefold()
    except Exception:
        html = ""
    if any(marker in html for marker in CAPTCHA_EVIDENCE_MARKERS):
        return True
    # Frase de verificação SÓ vale junto de form de verificação (dupla evidência).
    body = _page_text_snip(page, 5000)
    if VERIFY_PHRASE_RE.search(body) and any(
        t in html for t in ("checkpoint", "verification", "authwall")
    ):
        return True
    return False


def _wait_for_human_checkpoint(page, *, minutes: int) -> bool:
    """Return True if checkpoint cleared within the wait window."""
    deadline = time.time() + max(1, minutes) * 60
    # Hybrid captcha (pydoll): tenta Turnstile com clique humanizado ANTES de
    # incomodar o humano. reCAPTCHA/puzzles seguem com você (limites do pydoll).
    try:
        from hybrid_captcha import try_solve_turnstile

        if try_solve_turnstile(page) and not _page_has_checkpoint(page):
            LOG.info("Checkpoint resolvido por hybrid automation (turnstile).")
            return True
    except Exception as exc:
        LOG.debug("hybrid checkpoint: %s", exc)
    LOG.warning(
        "LinkedIn checkpoint/CAPTCHA detected — resolve it in the Chrome window "
        "(up to %s min). Automation paused.",
        minutes,
    )
    while time.time() < deadline:
        time.sleep(5)
        if not _page_has_checkpoint(page) and not _page_looks_logged_out(page):
            _human_pause(1.0, 2.0)
            return True
    return False


def _wait_for_manual_login(page, *, minutes: int, profile: str) -> bool:
    """Block until LinkedIn session looks logged in, or timeout."""
    deadline = time.time() + max(3, minutes) * 60
    LOG.warning(
        "LinkedIn login necessário — faça login no Chrome (perfil %s). "
        "Aguardando até %s min. Não feche a janela; o robô não avança sem sessão.",
        profile,
        minutes,
    )
    while time.time() < deadline:
        try:
            if _page_has_checkpoint(page):
                remaining = max(1, int((deadline - time.time()) / 60))
                if not _wait_for_human_checkpoint(page, minutes=min(10, remaining)):
                    return False
            if not _page_looks_logged_out(page):
                _human_pause(1.5, 3.0)
                if not _page_looks_logged_out(page):
                    LOG.info("LinkedIn login detectado; seguindo para a vaga.")
                    return True
        except Exception as exc:
            LOG.warning("Login wait poll error (continuando): %s", exc)
        time.sleep(4)
    return False


def last_linkedin_action_at(connect_fn: ConnectFn) -> datetime | None:
    with connect_fn() as db:
        row = db.execute(
            """SELECT attempted_at FROM applications
               WHERE channel='linkedin'
               ORDER BY attempted_at DESC LIMIT 1"""
        ).fetchone()
    if not row or not row["attempted_at"]:
        return None
    raw = str(row["attempted_at"]).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def rate_limit_block_reason(connect_fn: ConnectFn, cfg: dict[str, str]) -> str | None:
    """Return a human-readable block reason if min-gap pacing would be exceeded.

    No daily cap — only spacing between LinkedIn Easy Apply actions.
    """
    min_gap = _int_setting(
        cfg,
        "linkedin_min_gap_minutes",
        DEFAULT_MIN_GAP_MINUTES,
        lo=HARD_MIN_GAP_MINUTES,
        hi=HARD_MAX_GAP_MINUTES,
    )
    now = datetime.now(timezone.utc)
    last = last_linkedin_action_at(connect_fn)
    if last is not None:
        elapsed = (now - last.astimezone(timezone.utc)).total_seconds() / 60.0
        if elapsed < min_gap:
            wait = int(min_gap - elapsed) + 1
            return (
                f"Intervalo mínimo entre Easy Apply: {min_gap} min "
                f"(faltam ~{wait} min). Perfil/Chrome sequencial."
            )
    return None


def _click_first_matching(page, pattern: re.Pattern, *, timeout: float = 4000) -> bool:
    buttons = page.get_by_role("button")
    count = buttons.count()
    for i in range(min(count, 40)):
        btn = buttons.nth(i)
        try:
            if not btn.is_visible(timeout=200):
                continue
            label = (btn.inner_text(timeout=500) or "").strip()
            aria = (btn.get_attribute("aria-label") or "").strip()
            hay = f"{label} {aria}".strip()
            if pattern.search(hay):
                # Prefer real click with small mouse settle, not instant JS click.
                btn.scroll_into_view_if_needed(timeout=2000)
                _human_pause(0.2, 0.6)
                btn.click(timeout=timeout, delay=random.randint(40, 140))
                return True
        except Exception:
            continue
    return False


def _find_easy_apply_button(page):
    candidates = [
        page.get_by_role("button", name=EASY_APPLY_RE),
        page.locator('button[aria-label*="Easy Apply" i]'),
        page.locator('button[aria-label*="Candidatura simplificada" i]'),
        page.locator("button").filter(has_text=EASY_APPLY_RE),
    ]
    for loc in candidates:
        try:
            if loc.count() and loc.first.is_visible(timeout=800):
                return loc.first
        except Exception:
            continue
    return None


def _find_external_apply_button(page):
    """LinkedIn 'Apply' that leaves the site (not Easy Apply)."""
    buttons = page.get_by_role("button")
    count = buttons.count()
    for i in range(min(count, 40)):
        btn = buttons.nth(i)
        try:
            if not btn.is_visible(timeout=200):
                continue
            label = (btn.inner_text(timeout=400) or "").strip()
            aria = (btn.get_attribute("aria-label") or "").strip()
            hay = f"{label} {aria}".strip()
            if EASY_APPLY_RE.search(hay):
                continue
            if EXTERNAL_APPLY_BTN_RE.search(hay) or EXTERNAL_APPLY_RE.search(hay):
                return btn
        except Exception:
            continue
    for loc in (
        page.locator("a").filter(has_text=EXTERNAL_APPLY_BTN_RE),
        page.locator('a[href*="inhire"]').filter(has_text=re.compile(r"apply|candidat", re.I)),
    ):
        try:
            if loc.count() and loc.first.is_visible(timeout=500):
                return loc.first
        except Exception:
            continue
    return None


def _salary_from_rules(rules: list[dict], cfg: dict[str, str], job_text: str = "") -> str:
    for rule in rules:
        if str(rule.get("key") or "").casefold() == "salary":
            value = resolve_rule_value(rule, cfg, field_hint=job_text, job_text=job_text) or ""
            return str(value).strip()
    return ""


def _open_external_apply_target(page, context, apply_btn):
    """Click external Apply; return the page that should receive the ATS form."""
    before = list(context.pages)
    try:
        with context.expect_page(timeout=15000) as new_page_info:
            apply_btn.click(timeout=8000, delay=random.randint(40, 140))
        target = new_page_info.value
        try:
            target.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        _human_pause(1.5, 3.0)
        return target
    except Exception:
        try:
            apply_btn.click(timeout=8000, delay=random.randint(40, 140))
        except Exception:
            pass
        _human_pause(2.0, 4.0)
        for p in context.pages:
            try:
                if p not in before and p != page:
                    try:
                        p.wait_for_load_state("domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                    return p
            except Exception:
                continue
        return page


def _handle_external_ats(
    *,
    page,
    context,
    cfg: dict[str, str],
    rules: list[dict],
    resume_path: str,
    cover_letter: str,
    human_wait: int,
    finish_ok,
    block,
    ai: dict | None = None,
) -> tuple[bool, str]:
    """Deixa o LinkedIn e espera a URL de destino; o router decide o handler."""
    from ats_router import (
        OUTCOME_ASSISTED,
        OUTCOME_FAILED,
        OUTCOME_NO_HANDLER,
        OUTCOME_SUBMITTED,
        OUTCOME_TIMEOUT,
        find_handler,
        run_ats_flow,
    )

    # O redirect passa por linkedin.com/me/events antes do host final: poll curto.
    deadline = time.monotonic() + 10
    current = page.url or ""
    while time.monotonic() < deadline:
        try:
            current = page.url or ""
        except Exception:
            current = ""
        if find_handler(current) is not None:
            break
        if current and "linkedin.com" not in current:
            break
        _human_pause(0.7, 1.2)
    else:
        # URL ainda no LinkedIn? varre abas (popup abriu fora daqui).
        for p in context.pages:
            try:
                if find_handler(p.url or "") is not None:
                    page = p
                    current = p.url or ""
                    try:
                        page.bring_to_front()
                    except Exception:
                        pass
                    break
            except Exception:
                continue

    salary = _salary_from_rules(rules, cfg, job_context_text((ai or {}).get("job")))
    outcome, detail = run_ats_flow(
        page,
        context,
        cfg=cfg,
        rules=rules,
        resume_path=resume_path,
        cover_letter=cover_letter,
        salary=salary,
        human_wait=human_wait,
        ai=ai,
    )

    try:
        context.close()
    except Exception:
        pass

    if outcome == OUTCOME_SUBMITTED:
        return finish_ok(
            f"auto: {detail}",
            status_detail=f"Candidatura enviada pelo robô — {detail}",
        )
    if outcome == OUTCOME_ASSISTED:
        return finish_ok(
            f"assisted: {detail}",
            status_detail=detail or "ATS preenchido; você enviou (assistido).",
        )
    if outcome == OUTCOME_TIMEOUT:
        return finish_ok(
            f"dry-run: {detail}",
            status_detail=detail or "ATS preenchido; sem confirmacao de envio.",
        )
    if outcome == OUTCOME_FAILED:
        return block(f"Falha no ATS externo: {detail}")
    # OUTCOME_NO_HANDLER: app.py manda para 'worth' e a nota carrega NO_HANDLER:
    return block(detail)


_MODAL_SELECTORS = (
    '[role="dialog"]',
    ".jobs-easy-apply-modal",
    "div.artdeco-modal",
)


def _visible_modal(page, *, limit: int = 6):
    """Primeiro diálogo/overlay REALMENTE visível, não apenas o 1º match.

    O LinkedIn mantém dialogs ocultos montados no DOM (typeahead, notificações)
    que aparecem ANTES do modal da candidatura: `.first` congelava num desses e
    o wait expirava com o modal aberto na tela (reproduzido no shim do pydoll).
    Nota: ':visible' não existe no pydoll — daí a varredura por nth().
    """
    for sel in _MODAL_SELECTORS:
        try:
            loc = page.locator(sel)
            count = min(int(loc.count()), limit)
        except Exception:
            continue
        for i in range(count):
            cand = loc.nth(i)
            try:
                if cand.is_visible(timeout=300):
                    return cand
            except Exception:
                continue
    return None


def _modal(page, *, timeout_ms: float = 10000):
    """Polling que REAVALIA o DOM até algum modal ficar visível; None = não abriu.

    Diferente do antigo `.first` congelado: o modal do Easy Apply monta depois do
    clique; varrer de novo a cada round é o que pega o dialog certo.
    """
    deadline = time.monotonic() + max(0.0, timeout_ms) / 1000.0
    while True:
        found = _visible_modal(page)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        _human_pause(0.25, 0.5)


def _collect_controls(modal) -> list[dict]:
    return modal.evaluate(
        """(root) => {
          const out = [];
          const nodes = root.querySelectorAll('input, textarea, select');
          for (const el of nodes) {
            const type = (el.type || '').toLowerCase();
            if (type === 'hidden' || type === 'submit' || type === 'button' || el.disabled) continue;
            const id = el.id || '';
            let label = '';
            if (id) {
              const lab = root.querySelector(`label[for="${CSS.escape(id)}"]`);
              if (lab) label = lab.innerText || '';
            }
            if (!label && el.closest('label')) label = el.closest('label').innerText || '';
            if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');
            if (!label) {
              const legend = el.closest('fieldset')?.querySelector('legend');
              if (legend) label = legend.innerText || '';
            }
            if (!label) label = [el.name, el.placeholder, el.id].filter(Boolean).join(' ');
            const options = el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text) : [];
            out.push({
              tag: el.tagName.toLowerCase(),
              type,
              name: el.name || '',
              id,
              placeholder: el.placeholder || '',
              label: (label || '').slice(0, 400),
              options,
            });
          }
          return out;
        }"""
    )


def _fill_modal_step(
    *,
    page,
    modal,
    rules: list[dict],
    cfg: dict[str, str],
    job: dict,
    cover_letter: str,
    resume_path: str,
    resume_summary: str,
    resume_json: str,
    facts: str,
    provider: str,
    model: str,
    api_key: str,
    pending_answers: list[tuple[str, str]],
    open_count: list[int],
    ai_ctx: dict | None = None,
    now_iso: str = "",
    connect_fn=None,
) -> str | None:
    """Fill one Easy Apply step. Returns error detail or None on success."""
    # perguntas com opcoes (radio/checkbox) do wizard: diversidade->perfil,
    # empresa->IA com cache global; insegura fica com o humano.
    if ai_ctx is not None:
        try:
            from ats_answers import answer_choice_groups
            from ats_base import ApplyContext

            step_ctx = ApplyContext(
                cfg=cfg, rules=rules, resume_path=resume_path,
                cover_letter=cover_letter, ai=ai_ctx,
            )
            answered, _deferred = answer_choice_groups(modal, step_ctx, log_prefix="easy-apply")
            pending_answers.extend(("opcao respondida", a[:500]) for a in answered)
        except Exception as exc:
            LOG.debug("grupos de opcoes no modal falharam: %s", exc)

    controls = _collect_controls(modal)
    for control in controls:
        label = control.get("label") or control.get("name") or ""
        tag = control.get("tag")
        ctype = control.get("type")
        rule = find_rule_for_label(label, rules)

        locator = None
        if control.get("name"):
            locator = modal.locator(f'[name="{control["name"]}"]').first
        elif control.get("id"):
            locator = modal.locator(f'#{control["id"]}').first

        if rule and str(rule["mode"]) == "skip":
            continue

        if rule is None and lookslike_open_question(label, tag if tag == "textarea" else "input"):
            open_count[0] += 1
            if open_count[0] > 6:
                return "Muitas perguntas abertas; revise manualmente no Chrome."
            try:
                from ats_answers import ask_open_cached

                answer = ask_open_cached(
                    question=label,
                    generate=lambda: generate_open_answer(
                        question=label,
                        job=job,
                        resume_summary=resume_summary,
                        resume_json=resume_json,
                        facts=facts,
                        provider=provider,
                        model=model,
                        api_key=api_key,
                    ),
                    connect_fn=connect_fn,
                    provider=provider,
                    model=model,
                    now_iso=now_iso,
                )
            except AiUnavailableError as exc:
                return f"Pergunta aberta; IA indisponível — preencha no Chrome. {exc}"
            except Exception as exc:
                return f"Não foi possível sugerir resposta: {exc}"
            pending_answers.append((label[:500], answer))
            if locator:
                try:
                    _human_type(locator, answer)
                    _human_pause(0.4, 1.0)
                except Exception:
                    pass
            continue

        if rule is None:
            continue

        field_hint = " ".join(
            str(control.get(k) or "") for k in ("label", "placeholder", "name", "id")
        )
        value = resolve_rule_value(rule, cfg, cover_letter=cover_letter, resume_path=resume_path, field_hint=field_hint, job_text=job_context_text(job))
        if value is None or value == "":
            if str(rule["mode"]) in {"select", "salary", "file"}:
                return f"Campo sem valor configurado: {rule['key']} — preencha no Chrome."
            continue
        if str(value).strip().casefold() in {"ask", "both", "nao_informado", "not_informed"}:
            continue  # decisao reservada ao humano no painel
        value = prepare_text_value(rule, value, field_hint)
        if not locator:
            continue

        mode = str(rule["mode"])
        try:
            if mode == "file" or ctype == "file":
                locator.set_input_files(resume_path)
                _human_pause(0.5, 1.2)
            elif tag == "select" or mode == "select":
                options = control.get("options") or []
                # faixa de select: texto digitado na regra vence; senao casa o
                # valor resolvido ("R$ 6.000" ⊂ "R$ 5.000 - R$ 7.000").
                preferred = str(rule.get("value") or "").strip() or str(value)
                chosen = pick_select_option(list(options), preferred)
                if not chosen:
                    return f"Select sem opção compatível para {rule['key']} (valor: {value})"
                locator.select_option(label=chosen)
                _human_pause(0.3, 0.8)
            elif ctype in {"radio", "checkbox"}:
                continue
            else:
                # Skip if already looks filled with the same value.
                try:
                    current = (locator.input_value(timeout=800) or "").strip()
                except Exception:
                    current = ""
                if current and current.casefold() == str(value).strip().casefold():
                    continue
                _human_type(locator, str(value))
                _human_pause(0.35, 0.9)
        except Exception as exc:
            LOG.warning("Easy Apply field fill failed for %s: %s", rule.get("key"), exc)
    return None


def _save_pending_answers(
    connect_fn: ConnectFn,
    *,
    job_id: int,
    pending_answers: list[tuple[str, str]],
    provider: str,
    model: str,
    now_iso: str,
) -> None:
    if not pending_answers:
        return
    with connect_fn() as db:
        for question, answer in pending_answers:
            db.execute(
                """INSERT INTO form_answers(job_id,question,answer,provider,model,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (job_id, question, answer, provider, model, now_iso),
            )


def _wait_for_human_submit(page, *, minutes: int) -> str:
    """
    Leave the Review step open. You click Submit (or dismiss).
    Returns: 'submitted' | 'abandoned' | 'timeout'
    """
    deadline = time.time() + max(1, minutes) * 60
    LOG.info(
        "Assisted Easy Apply: formulário pronto. Clique em Enviar no Chrome "
        "(até %s min). O robô NÃO envia sozinho.",
        minutes,
    )
    while time.time() < deadline:
        time.sleep(4)
        snip = _page_text_snip(page, 4000)
        if SUCCESS_RE.search(snip):
            return "submitted"
        if _page_has_checkpoint(page):
            if not _wait_for_human_checkpoint(page, minutes=min(8, minutes)):
                return "abandoned"
            continue
        # Modal gone without success text → user closed or navigated away.
        try:
            modal = page.locator('[role="dialog"]').first
            if not modal.is_visible(timeout=400):
                # Brief settle then re-check success toast on main page.
                _human_pause(0.8, 1.5)
                if SUCCESS_RE.search(_page_text_snip(page, 4000)):
                    return "submitted"
                if ALREADY_RE.search(_page_text_snip(page, 3000)):
                    return "submitted"
                return "abandoned"
        except Exception:
            pass
    return "timeout"


def apply_via_linkedin(
    connect_fn: ConnectFn,
    job: dict,
    cfg: dict[str, str],
    *,
    cover_letter: str,
    resume_path: str,
    resume_id: int,
    cover_letter_id: int | None,
    resume_summary: str,
    resume_json: str,
    provider: str,
    model: str,
    api_key: str,
    now_iso: str,
    project_root: str,
) -> tuple[bool, str]:
    """
    Assisted Easy Apply only:
    - requires risk acknowledgement
    - never clicks Submit
    - min-gap pacing only (no daily cap); queue runs at most one at a time
    - headed Chrome persistent profile (manual login once)
    - leaves Review open for you to send
    """
    if (cfg.get("linkedin_easy_apply") or "0") != "1":
        return False, "Easy Apply LinkedIn desligado nas configurações."
    if (cfg.get("linkedin_risk_ack") or "0") != "1":
        return (
            False,
            "Confirme o aviso de risco/ToS no painel (checkbox) antes de usar Easy Apply.",
        )

    try:
        from browser_engine import engine_install_hint, resolve_sync_playwright

        module = resolve_sync_playwright(cfg)
        sync_playwright = module.sync_playwright
    except ImportError:
        return False, (
            "Motor de navegador indisponível. "
            f"Instale com: {engine_install_hint(cfg)}"
        )

    limit_reason = rate_limit_block_reason(connect_fn, cfg)
    if limit_reason:
        # Gap → let the job queue retry later.
        raise RuntimeError(f"LinkedIn pacing — try again later. {limit_reason}")

    profile = (cfg.get("linkedin_chrome_profile") or "").strip() or default_profile_dir(project_root)
    os.makedirs(profile, exist_ok=True)
    human_wait = _int_setting(
        cfg, "linkedin_human_wait_minutes", DEFAULT_HUMAN_WAIT_MINUTES, lo=3, hi=45
    )
    login_wait = _int_setting(
        cfg, "linkedin_login_wait_minutes", DEFAULT_LOGIN_WAIT_MINUTES, lo=5, hi=60
    )
    url = _normalize_job_url(job.get("url") or "")
    if not url or "linkedin.com" not in url.casefold():
        return False, "URL não é uma vaga LinkedIn."

    acquired = _LINKEDIN_LOCK.acquire(timeout=_LOCK_WAIT_SECONDS)
    if not acquired:
        return False, "Outra candidatura LinkedIn ainda em andamento (perfil Chrome exclusivo)."

    def block(detail: str, *, status: str = "blocked") -> tuple[bool, str]:
        _insert_application(
            connect_fn,
            job_id=job["id"],
            channel="linkedin",
            recipient="",
            resume_id=resume_id,
            cover_letter_id=cover_letter_id,
            status=status,
            detail=detail,
            now_iso=now_iso,
        )
        return False, detail

    def finish_ok(detail: str, *, status_detail: str) -> tuple[bool, str]:
        _save_pending_answers(
            connect_fn,
            job_id=job["id"],
            pending_answers=pending_answers,
            provider=provider,
            model=model,
            now_iso=now_iso,
        )
        _insert_application(
            connect_fn,
            job_id=job["id"],
            channel="linkedin",
            recipient="",
            resume_id=resume_id,
            cover_letter_id=cover_letter_id,
            status="sent",
            detail=status_detail,
            now_iso=now_iso,
        )
        return True, detail

    with connect_fn() as db:
        rules = [dict(row) for row in list_rules(db)]
    facts = cfg.get("candidate_facts_pt" if job.get("language") == "pt" else "candidate_facts_en", "")
    # contexto p/ IA responder perguntas de empresa (radio/checkbox e abertas)
    # reaproveitando a ANALISE do curriculo ja persistida + cache global.
    ai_ctx: dict = {
        "job": job,
        "resume_summary": resume_summary,
        "resume_json": resume_json,
        "facts": facts,
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "connect_fn": connect_fn,
        "now_iso": now_iso,
    }
    pending_answers: list[tuple[str, str]] = []
    open_count = [0]
    context = None
    try:
        with sync_playwright() as playwright:
            from browser_engine import BACKGROUND_RUN_ARGS, persistent_launch_kwargs

            launch_kwargs = persistent_launch_kwargs(
                profile,
                headless=False,
                slow_mo=random.randint(40, 90),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                # Mesmo da reverificação: SPA não trava com a janela atrás de outras.
                args=list(BACKGROUND_RUN_ARGS),
            )
            try:
                context = playwright.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            except Exception:
                context = playwright.chromium.launch_persistent_context(**launch_kwargs)

            page = context.pages[0] if context.pages else context.new_page()

            # URL de ATS conhecido direto (sem hop LinkedIn) — handler decide.
            if find_handler(url) is not None:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                _human_pause(1.5, 3.0)
                return _handle_external_ats(
                    page=page,
                    context=context,
                    cfg=cfg,
                    rules=rules,
                    resume_path=resume_path,
                    cover_letter=cover_letter,
                    human_wait=human_wait,
                    finish_ok=finish_ok,
                    block=block,
                    ai=ai_ctx,
                )

            # Go straight to the LinkedIn job URL.
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _human_pause(1.8, 3.5)

            if _page_has_checkpoint(page):
                if not _wait_for_human_checkpoint(page, minutes=min(15, login_wait)):
                    context.close()
                    return block(
                        "Checkpoint/CAPTCHA não resolvido a tempo; faça login no perfil Chrome "
                        "e clique Easy Apply de novo. Nada foi enviado."
                    )

            if _page_looks_logged_out(page):
                if not _wait_for_manual_login(page, minutes=login_wait, profile=profile):
                    # Closing saves cookies from any partial login progress.
                    context.close()
                    return block(
                        "LinkedIn pediu login e o tempo esgotou. "
                        "Conclua o login no perfil Chrome (mantenha a sessão) e clique Easy Apply de novo. "
                        f"Perfil: {profile}"
                    )
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                _human_pause(1.5, 3.0)
                if _page_looks_logged_out(page):
                    context.close()
                    return block(
                        "Ainda sem sessão LinkedIn após login. "
                        "Confirme que entrou na conta certa neste perfil Chrome e tente Easy Apply de novo."
                    )

            body_snip = _page_text_snip(page, 4000)
            if (
                ALREADY_RE.search(body_snip)
                and not EASY_APPLY_RE.search(body_snip)
                and not EXTERNAL_APPLY_RE.search(body_snip)
            ):
                context.close()
                return block("Já candidatado nesta vaga (LinkedIn).")

            # Light scroll so the apply button is in view like a real reader.
            try:
                page.mouse.wheel(0, random.randint(200, 500))
                _human_pause(0.6, 1.4)
            except Exception:
                pass

            easy_btn = _find_easy_apply_button(page)
            external_btn = None if easy_btn else _find_external_apply_button(page)

            if not easy_btn and not external_btn:
                if _page_looks_logged_out(page):
                    if not _wait_for_manual_login(page, minutes=login_wait, profile=profile):
                        context.close()
                        return block(
                            "Sem botão Apply e sem sessão LinkedIn. "
                            "Faça login neste perfil Chrome e clique Easy Apply de novo."
                        )
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    _human_pause(1.5, 3.0)
                    easy_btn = _find_easy_apply_button(page)
                    external_btn = None if easy_btn else _find_external_apply_button(page)
                if not easy_btn and not external_btn:
                    context.close()
                    return block(
                        "Nenhum botão Easy Apply nem Apply externo encontrado. "
                        "Nada enviado automaticamente."
                    )

            # --- External ATS (InHire, etc.) ---
            if external_btn and not easy_btn:
                external_btn.scroll_into_view_if_needed(timeout=3000)
                _human_pause(0.4, 1.0)
                ats_page = _open_external_apply_target(page, context, external_btn)
                return _handle_external_ats(
                    page=ats_page,
                    context=context,
                    cfg=cfg,
                    rules=rules,
                    resume_path=resume_path,
                    cover_letter=cover_letter,
                    human_wait=human_wait,
                    finish_ok=finish_ok,
                    block=block,
                    ai=ai_ctx,
                )

            easy_btn.scroll_into_view_if_needed(timeout=3000)
            _human_pause(0.4, 1.0)
            easy_btn.click(timeout=8000, delay=random.randint(50, 160))
            _human_pause(1.0, 2.0)

            try:
                if find_handler(page.url or "") is not None:
                    return _handle_external_ats(
                        page=page,
                        context=context,
                        cfg=cfg,
                        rules=rules,
                        resume_path=resume_path,
                        cover_letter=cover_letter,
                        human_wait=human_wait,
                        finish_ok=finish_ok,
                        block=block,
                        ai=ai_ctx,
                    )
            except Exception:
                pass

            # Multi-step: fill + Next/Review only. NEVER click Submit.
            max_steps = 10
            reached_review = False
            for step in range(max_steps):
                if _page_has_checkpoint(page):
                    if not _wait_for_human_checkpoint(page, minutes=min(8, human_wait)):
                        context.close()
                        return block("Checkpoint durante o wizard; parado sem enviar.")

                modal = _modal(page)
                if modal is None:
                    for p in context.pages:
                        try:
                            if find_handler(p.url or "") is not None:
                                return _handle_external_ats(
                                    page=p,
                                    context=context,
                                    cfg=cfg,
                                    rules=rules,
                                    resume_path=resume_path,
                                    cover_letter=cover_letter,
                                    human_wait=human_wait,
                                    finish_ok=finish_ok,
                                    block=block,
                                    ai=ai_ctx,
                                )
                        except Exception:
                            continue
                    context.close()
                    return block("Modal Easy Apply não abriu.", status="failed")

                err = _fill_modal_step(
                    page=page,
                    modal=modal,
                    rules=rules,
                    cfg=cfg,
                    job=job,
                    cover_letter=cover_letter,
                    resume_path=resume_path,
                    resume_summary=resume_summary,
                    resume_json=resume_json,
                    facts=facts,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    pending_answers=pending_answers,
                    open_count=open_count,
                    ai_ctx=ai_ctx,
                    now_iso=now_iso,
                    connect_fn=connect_fn,
                )
                if err:
                    # Travou no modal Easy Apply: o copiloto assume com override total.
                    from ats_router import copilot_rescue

                    cstate, cnote, cpage = copilot_rescue(page, context, cfg, ai_ctx,
                                                          reason="modal Easy Apply travado — " + str(err))
                    if cstate == "aborted":
                        return block(cnote)
                    if cstate == "solved":
                        page = cpage
                        retry_modal = _modal(page, timeout_ms=4000)
                        if retry_modal is None:
                            retry_modal = _visible_modal(page) or page
                        err2 = _fill_modal_step(
                            page=page, modal=retry_modal, rules=rules, cfg=cfg, job=job,
                            cover_letter=cover_letter, resume_path=resume_path,
                            resume_summary=resume_summary, resume_json=resume_json,
                            facts=facts, provider=provider, model=model, api_key=api_key,
                            pending_answers=pending_answers, open_count=open_count,
                            ai_ctx=ai_ctx, now_iso=now_iso, connect_fn=connect_fn,
                        )
                        if not err2:
                            err = None
                    if err:
                        # Copiloto nao resolveu (ou nao havia IA): browser aberto p/ humano.
                        LOG.warning("Fill incomplete (%s) — aguardando você no Chrome.", err)
                        outcome = _wait_for_human_submit(page, minutes=human_wait)
                        try:
                            context.close()
                        except Exception:
                            pass
                        if outcome == "submitted":
                            return finish_ok(
                                "assisted: você enviou após preenchimento parcial.",
                                status_detail="Easy Apply enviado por você (assistido, fill parcial).",
                            )
                        return block(f"{err} (Chrome ficou aberto para correção; sem envio automático.)")

                _human_pause(0.7, 1.6)

                # Prefer Review; never Submit.
                if _click_first_matching(page, REVIEW_RE):
                    reached_review = True
                    _human_pause(1.0, 2.0)
                    break

                if _click_first_matching(page, NEXT_RE):
                    _human_pause(0.9, 2.0)
                    continue

                # Single-step form with only Submit visible → stop for human.
                reached_review = True
                break
            else:
                context.close()
                return block("Easy Apply excedeu passos sem chegar em Review.", status="failed")

            if not reached_review:
                context.close()
                return block("Não chegou à etapa de revisão.", status="failed")

            outcome = _wait_for_human_submit(page, minutes=human_wait)
            try:
                context.close()
            except Exception:
                pass

            if outcome == "submitted":
                return finish_ok(
                    "assisted: candidatura enviada por você no Chrome.",
                    status_detail="Easy Apply enviado por você (modo assistido; robô não clicou Enviar).",
                )
            if outcome == "timeout":
                return finish_ok(
                    "dry-run: formulário preenchido; tempo esgotado sem você confirmar o envio.",
                    status_detail="Easy Apply preenchido; timeout sem envio (robô nunca clica Enviar).",
                )
            return finish_ok(
                "dry-run: formulário preenchido; janela fechada sem confirmação de envio.",
                status_detail="Easy Apply preenchido; modal fechado sem sucesso (robô nunca clica Enviar).",
            )
    except Exception as exc:
        try:
            if context:
                context.close()
        except Exception:
            pass
        return block(f"Falha no Easy Apply assistido: {exc}", status="failed")
    finally:
        _LINKEDIN_LOCK.release()