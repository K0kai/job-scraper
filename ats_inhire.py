# InHire (inhire.app) ATS — handler da arquitetura ats_base.
# NUNCA resolve captcha nem clica "Continue registration" (final) — assisted-first.
# quirks mapeados: dropdowns React (aria-label="Dropdown select"), telefone BR com
# +55 ANTES do número, país "Brazil (BR)" em UI inglesa, diversidade PCD, EULA.
from __future__ import annotations

import logging
import random
import re
import time

from ats_base import BaseATSHandler, FillResult, register
from form_rules import detect_contract_regime, detect_salary_currency, format_cpf, job_context_text, salary_for

LOG = logging.getLogger("job-scraper")

SUCCESS_RE = re.compile(
    r"candidatura\s+enviada|application\s+sent|obrigad[oa]|recebemos\s+sua|"
    r"inscri[cç][aã]o\s+enviada|sucesso|registration\s+sent|continue\s+registration",
    re.I,
)

# evidência: https://acme.inhire.app jobs reais (sessão Easy Apply, 2026-09)
NAME_FIELD_SELECTOR = "#name, input[name='name']"
CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha", "cf-challenge")


def _pause(lo: float = 0.3, hi: float = 0.9) -> None:
    time.sleep(random.uniform(lo, hi))


def _linkedin_profile_value(cfg: dict[str, str]) -> str:
    raw = (cfg.get("candidate_linkedin") or "").strip()
    if not raw:
        return ""
    if "linkedin.com" not in raw.casefold():
        return f"https://www.linkedin.com/in/{raw.lstrip('/')}"
    if not raw.startswith("http"):
        return "https://" + raw
    return raw


def _local_phone_digits(cfg: dict[str, str]) -> str:
    """National number only — dial code is chosen in the phone-country dropdown first."""
    digits = re.sub(r"\D+", "", cfg.get("candidate_phone") or "")
    if digits.startswith("55") and len(digits) >= 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) > 10:
        digits = digits.lstrip("0")
    return digits


def _salary_inhire(page, cfg: dict[str, str], ctx) -> str:
    """Pretensão para o campo de salário do InHire.

    A moeda é inferida do placeholder real do campo (R$ 0.000,00 → BRL,
    '$' → USD); sem campo/mostrador, 'auto' cai no preferido do painel. O BRL
    já sai multiplicado quando a contratação preferida é PJ (ver salary_for).
    """
    hint = ""
    try:
        loc = page.locator("#salaryExpectation, input[name='salaryExpectation']").first
        if loc.count():
            hint = " ".join(
                str(loc.get_attribute(k) or "") for k in ("placeholder", "name")
            )
    except Exception:
        hint = ""
    currency = detect_salary_currency(hint, preferred=cfg.get("salary_currency_preference", ""))
    # Regime CLT×PJ detectado na vaga/página vence a preferência do painel.
    job = (ctx.ai or {}).get("job") if ctx.ai else None
    regime = detect_contract_regime(hint, job_context_text(job))
    value = salary_for(cfg, currency, regime=regime)
    if not value and currency != "BRL":
        value = salary_for(cfg, "BRL", regime=regime)
    if not value:  # legado: algum campo livre ainda alimenta ctx.salary
        value = (ctx.salary or cfg.get("linkedin_salary_expectation") or "").strip()
    return value


def _prefer_english_ui(page) -> None:
    """Country list uses 'Brazil (BR)' in English; prefer that for reliable matching."""
    try:
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            try:
                options = sel.locator("option").all_inner_texts()
            except Exception:
                continue
            joined = " ".join(options).casefold()
            if "english" in joined or any(o.strip().casefold() == "en" for o in options):
                try:
                    sel.select_option(value="en")
                except Exception:
                    try:
                        sel.select_option(label="English")
                    except Exception:
                        continue
                _pause(1.0, 1.8)
                return
    except Exception as exc:
        LOG.info("InHire language switch skipped: %s", exc)


def _select_country_brazil(page) -> bool:
    """Type/select Brazil (not Brasil) — works on English UI as 'Brazil (BR)'."""
    if _select_react_dropdown(
        page,
        matcher=r"Select a country|Country of origin|Selecione o pa[ií]s",
        option_query="Brazil",
        option_regex=r"Brazil\s*\(BR\)|^Brazil$",
    ):
        return True
    # Open any country placeholder and pick Brazil (BR) from filtered list.
    try:
        dd = page.locator('[aria-label="Dropdown select"]').filter(
            has_text=re.compile(r"country|pa[ií]s|Select a country|Selecione o", re.I)
        )
        if not dd.count():
            return False
        dd.first.click(force=True)
        _pause(0.2, 0.4)
        page.keyboard.type("Brazil", delay=30)
        _pause(0.5, 0.9)
        opt = page.locator(
            ".react-dropdown-select-dropdown button, "
            ".react-dropdown-select-dropdown [role='option']"
        ).filter(has_text=re.compile(r"Brazil", re.I))
        if opt.count():
            opt.first.click(force=True)
            _pause(0.4, 0.8)
            return True
        page.keyboard.press("Enter")
        _pause(0.4, 0.8)
        return True
    except Exception as exc:
        LOG.warning("InHire country Brazil failed: %s", exc)
        return False


def _select_react_dropdown(
    page,
    *,
    matcher: str,
    option_query: str,
    option_regex: str | None = None,
    timeout: float = 8000,
    index: int | None = None,
) -> bool:
    """Open a react-dropdown-select, type a query, pick an option (force-click OK)."""
    if index is not None:
        dd = page.locator('[aria-label="Dropdown select"]').nth(index)
    else:
        dd = page.locator('[aria-label="Dropdown select"]').filter(has_text=re.compile(matcher, re.I))
        if not dd.count():
            all_dd = page.locator('[aria-label="Dropdown select"]')
            chosen = None
            for i in range(all_dd.count()):
                try:
                    if not all_dd.nth(i).is_visible(timeout=300):
                        continue
                    text = all_dd.nth(i).inner_text(timeout=500) or ""
                except Exception:
                    continue
                if re.search(matcher, text, re.I):
                    chosen = all_dd.nth(i)
                    break
            if chosen is None:
                return False
            dd = chosen
        else:
            # Prefer a visible match.
            picked = None
            for i in range(dd.count()):
                try:
                    if dd.nth(i).is_visible(timeout=300):
                        picked = dd.nth(i)
                        break
                except Exception:
                    continue
            dd = picked or dd.first
    try:
        dd.scroll_into_view_if_needed(timeout=3000)
        dd.click(timeout=timeout, force=True)
        _pause(0.2, 0.45)
        page.keyboard.type(option_query, delay=random.randint(20, 45))
        _pause(0.45, 0.9)
        opt_re = re.compile(option_regex or re.escape(option_query), re.I)
        opt = page.locator(
            ".react-dropdown-select-dropdown button, "
            ".react-dropdown-select-dropdown [role='option']"
        ).filter(has_text=opt_re)
        if opt.count():
            opt.first.click(timeout=timeout, force=True)
        else:
            page.keyboard.press("Enter")
        _pause(0.35, 0.7)
        return True
    except Exception as exc:
        LOG.warning("InHire dropdown (%s → %s) failed: %s", matcher, option_query, exc)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _select_phone_country_code(page, *, dial: str = "+55") -> bool:
    """Must run BEFORE typing the phone number."""
    try:
        phone_dd = page.locator('[aria-label="Dropdown select"]').nth(0)
        phone_dd.scroll_into_view_if_needed(timeout=3000)
        phone_dd.click(timeout=5000, force=True)
        _pause(0.2, 0.4)
        page.keyboard.type(dial, delay=random.randint(25, 50))
        _pause(0.45, 0.8)
        opt = page.locator(
            ".react-dropdown-select-dropdown button, "
            ".react-dropdown-select-dropdown [role='option']"
        ).filter(has_text=re.compile(re.escape(dial)))
        if opt.count():
            # Option may be aria-selected/disabled — Enter or force click.
            try:
                opt.first.click(timeout=2000, force=True)
            except Exception:
                page.keyboard.press("Enter")
        else:
            page.keyboard.press("Enter")
        _pause(0.35, 0.7)
        return True
    except Exception as exc:
        LOG.warning("InHire phone country code failed: %s", exc)
        return False


def _click_visible_choice(page, texts: tuple[str, ...], *, nth: int = 0) -> bool:
    for text in texts:
        loc = page.get_by_text(text, exact=True)
        try:
            visible = []
            for i in range(loc.count()):
                if loc.nth(i).is_visible(timeout=200):
                    visible.append(loc.nth(i))
            if not visible:
                continue
            idx = min(nth, len(visible) - 1)
            visible[idx].click(timeout=4000)
            _pause(0.2, 0.5)
            return True
        except Exception:
            continue
    return False


def _fill_if_present(page, selector: str, value: str) -> None:
    if not value:
        return
    loc = page.locator(selector).first
    try:
        if not loc.count():
            return
        loc.scroll_into_view_if_needed(timeout=2000)
        loc.click(timeout=3000)
        loc.fill(value, timeout=5000)
        _pause(0.2, 0.5)
    except Exception as exc:
        LOG.warning("InHire fill %s failed: %s", selector, exc)


_DD_QUESTIONS_JS = """() => {
  const dds = [...document.querySelectorAll('[aria-label="Dropdown select"]')];
  return dds.map((d, i) => {
    let n = d.parentElement, question = '';
    for (let up = 0; up < 6 && n && !question; up++) {
      let sib = n.previousElementSibling;
      for (let s = 0; s < 3 && sib; s++) {
        const t = (sib.innerText || '').replace(/\\s+/g, ' ').trim();
        if (t.length > 8 && t.includes('?')) { question = t.slice(0, 220); break; }
        sib = sib.previousElementSibling;
      }
      n = n.parentElement;
    }
    return { i, question };
  });
}"""

_DD_OPTIONS_JS = """() => [...document.querySelectorAll('.react-dropdown-select-dropdown button')]
  .map(b => (b.getAttribute('aria-label') || (b.innerText || '').split(String.fromCharCode(10))[0] || '').trim())
  .filter(Boolean)"""


def _dd_open(page, idx: int) -> list[str]:
    """Abre o dropdown React idx e devolve os titulos das opcoes (fecha com Esc)."""
    try:
        dd = page.locator('[aria-label="Dropdown select"]').nth(idx)
        dd.scroll_into_view_if_needed(timeout=2500)
        dd.click(force=True, timeout=4000)
        _pause(0.35, 0.7)
        opts = list(page.evaluate(_DD_OPTIONS_JS) or [])
        page.keyboard.press("Escape")
        _pause(0.2, 0.4)
        return opts
    except Exception:
        return []


def _dd_pick(page, idx: int, option_title: str) -> bool:
    try:
        dd = page.locator('[aria-label="Dropdown select"]').nth(idx)
        dd.click(force=True, timeout=4000)
        _pause(0.35, 0.7)
        opt = page.locator(".react-dropdown-select-dropdown button").filter(
            has_text=re.compile(re.escape(option_title[:60]))
        )
        if not opt.count():
            page.keyboard.press("Escape")
            return False
        opt.first.click(force=True, timeout=4000)
        _pause(0.3, 0.6)
        return True
    except Exception as exc:
        LOG.debug("InHire dd pick(%s,%s) falhou: %s", idx, option_title, exc)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _answer_diversity_dropdowns(page, cfg: dict[str, str]) -> list[str]:
    """Dropdowns React de identidade (genero/orientacao/raca/PCD) → PERFIL, nunca IA."""
    from ats_answers import classify_group, diversity_answer_for_options, profile_state

    answered: list[str] = []
    try:
        items = list(page.evaluate(_DD_QUESTIONS_JS) or [])
    except Exception:
        return answered
    for item in items:
        question = item.get("question") or ""
        if not question:
            continue
        kind = classify_group(question)
        if not kind.startswith("diversity:"):
            continue  # telefone/pais etc. tratados no proprio fluxo
        category = kind.split(":", 1)[1]
        if category == "other":
            continue
        # 'yes' generico e ambiguo (as opcoes sao identidades especificas) — so
        # responde se o painel trouxer orientacao especifica (gay/bisexual/...).
        if "orienta" in question.casefold() and profile_state(cfg, "lgbtq") == "yes":
            LOG.info("InHire orientacao sexual: 'sou LGBTI+' nao indica qual opcao; fica com voce.")
            continue
        idx = int(item.get("i", -1))
        options = _dd_open(page, idx)
        if not options:
            continue
        chosen = diversity_answer_for_options(cfg, category, options)
        if not chosen:
            LOG.info("InHire '%s': sem perfil p/ opcoes %s; fica com voce.", question[:60], options[:3])
            continue
        if _dd_pick(page, idx, chosen):
            answered.append(f"{question[:70]} → {chosen}")
    return answered


def _fill_diversity_step(page, *, cfg: dict[str, str], ctx=None) -> None:
    """Step 2: grupos de marcacao + dropdowns React de identidade + privacidade."""
    # Navigate to diversity if Next is available.
    next_btn = page.get_by_role("button", name=re.compile(r"^(Next|Avançar|Continue)$", re.I))
    try:
        if next_btn.count() and next_btn.first.is_enabled(timeout=1500):
            next_btn.first.click(timeout=5000)
            _pause(1.0, 1.8)
    except Exception:
        try:
            page.get_by_role("button", name=re.compile(r"Diversity|Diversidade", re.I)).first.click(force=True)
            _pause(0.8, 1.4)
        except Exception:
            pass

    # Grupo "voce pertence a um dos grupos?" (checkboxes) + possiveis radios da etapa.
    if ctx is not None:
        try:
            from ats_answers import answer_choice_groups

            answer_choice_groups(page, ctx, log_prefix="inhire-div")
        except Exception as exc:
            LOG.debug("InHire diversity choice groups: %s", exc)

    # Dropdowns React de identidade: genero/orientacao/raca/PCD — tudo por perfil.
    for label in _answer_diversity_dropdowns(page, cfg):
        LOG.info("InHire diversidade: %s", label)

    # Privacy agreement on diversity step.
    _fill_privacy_checkbox(page)


_DISABLED_STATE_JS = """(reSrc) => {
  const rex = new RegExp(reSrc, 'i');
  const btns = [...document.querySelectorAll('button')];
  for (const b of btns) {
    const t = (b.innerText || '').trim();
    if (!t || !rex.test(t)) continue;
    const r = b.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    return { found: true, disabled: !!b.disabled || b.getAttribute('aria-disabled') === 'true' };
  }
  return { found: false, disabled: false };
}"""

_CONTINUE_RE = r"^(continuar registro|continuar|concluir|next|pr[oó]xima|submit|enviar)$"


def _continue_disabled(page) -> bool:
    """True se existe botao de avancar/continuar visivel e DESABILITADO (falta algo)."""
    try:
        state = page.evaluate(_DISABLED_STATE_JS, _CONTINUE_RE)
        return bool(state.get("found") and state.get("disabled"))
    except Exception:
        return False


def _retry_missing(page, ctx) -> list[str]:
    """Botao desabilitado → re-olhar radios/checkboxes e dropdowns de diversidade."""
    found: list[str] = []
    try:
        from ats_answers import answer_choice_groups

        extra, _deferred = answer_choice_groups(page, ctx, log_prefix="inhire-retry")
        found.extend(extra)
    except Exception as exc:
        LOG.debug("InHire retry groups: %s", exc)
    try:
        found.extend(_answer_diversity_dropdowns(page, ctx.cfg))
    except Exception as exc:
        LOG.debug("InHire retry dropdowns: %s", exc)
    try:
        modal = _question_modal(page)
        if modal is not None:
            from ats_answers import answer_choice_groups

            extra, _deferred = answer_choice_groups(modal, ctx, log_prefix="inhire-retry-modal")
            found.extend(extra)
    except Exception as exc:
        LOG.debug("InHire retry modal: %s", exc)
    return found


def _fill_privacy_checkbox(page) -> None:
    try:
        box = page.locator("#privacyPolicy, input[name='privacyPolicy']")
        if box.count():
            if not box.first.is_checked():
                box.first.check(force=True)
                _pause(0.2, 0.4)
    except Exception as exc:
        LOG.warning("InHire privacy checkbox failed: %s", exc)


_MODAL_SELECTOR = 'div[role="dialog"], [class*="modal" i], [class*="popup" i]'
_MODAL_CONTINUE_RE = re.compile(
    r"^(pros(?:se)?guir|continuar|next|avan(?:c|\u00e7)ar|pr\u00f3xima|responder|confirm(?:ar)?)$",
    re.I,
)
_MODAL_SUBMIT_RE = re.compile(r"submit|enviar|candidatar|finalizar|apply|concluir", re.I)


def _question_modal(page):
    """Modal embutido de perguntas da empresa (aparece APOIS a diversidade/captcha)."""
    try:
        for cand in page.locator(_MODAL_SELECTOR).all():
            try:
                if not cand.is_visible(timeout=400):
                    continue
                if cand.locator('input[type="radio"], input[type="checkbox"], textarea').count():
                    return cand
            except Exception:
                continue
    except Exception:
        pass
    return None


def _modal_question(modal) -> str:
    """Pergunta atual do modal = primeiro titulo/paragrafo com texto."""
    for sel in ("h2, h3, [class*='question' i], label, p"):
        try:
            els = modal.locator(sel).all()
        except Exception:
            continue
        for el in els:
            try:
                t = (el.inner_text(timeout=400) or "").replace("\n", " ").strip()
            except Exception:
                continue
            if len(t) >= 12 and "?" in t:
                return t[:300]
        for el in els:
            try:
                t = (el.inner_text(timeout=400) or "").replace("\n", " ").strip()
            except Exception:
                continue
            if len(t) >= 15:
                return t[:300]
    return ""


def _answer_modal_texts(modal, ctx) -> int:
    """Textarea/input de texto do modal: diversidade→perfil, empresa→IA com cache."""
    from ats_answers import (
        DIVERSITY_RE,
        NUMBERISH_RE,
        ask_open_cached,
        classify_group,
        diversity_text_for,
    )

    ai = ctx.ai or {}
    answered = 0
    try:
        fields = modal.locator("textarea, input[type='text']").all()
    except Exception:
        return 0
    for el in fields:
        try:
            if not el.is_visible(timeout=400):
                continue
            name = (el.get_attribute("name") or "") + " " + (el.get_attribute("id") or "")
            if "recaptcha" in name.casefold():
                continue
            if (el.input_value(timeout=600) or "").strip():
                continue
            question = _modal_question(modal) or name
            if DIVERSITY_RE.search(question):
                # pedem numero/codigo ("ICD/CID do laudo") ou DESCRICAO de
                # necessidade — template canonico seria mentira; fica com humano.
                from ats_answers import DESCRIBE_RE, NUMBERISH_RE

                if NUMBERISH_RE.search(question) or DESCRIBE_RE.search(question):
                    continue
                # identidade em area de texto: SEMPRE o perfil, nunca IA
                category = classify_group(question).split(":", 1)[1]
                text = diversity_text_for(ctx.cfg, category, language=(ai.get("job") or {}).get("language") or "en")
                if not text:
                    continue
                el.click(timeout=3000)
                el.fill(text)
                answered += 1
                continue
            if not ai.get("api_key"):
                continue
            from apply_channels import generate_open_answer

            answer = ask_open_cached(
                question=question,
                generate=lambda q=question: generate_open_answer(
                    question=q,
                    job=ai.get("job") or {},
                    resume_summary=ai.get("resume_summary") or "",
                    resume_json=ai.get("resume_json") or "",
                    facts=ai.get("facts") or "",
                    provider=ai.get("provider") or "",
                    model=ai.get("model") or "",
                    api_key=ai.get("api_key") or "",
                ),
                connect_fn=ai.get("connect_fn"),
                provider=ai.get("provider") or "",
                model=ai.get("model") or "",
                now_iso=ai.get("now_iso") or "",
            )
            el.click(timeout=3000)
            el.fill(answer[:1200])
            answered += 1
        except Exception as exc:
            LOG.debug("InHire modal text answer: %s", exc)
    return answered


@register
class InHireHandler(BaseATSHandler):
    """Primeiro handler concreto da arquitetura — assistido (captcha no final)."""

    name = "inhire"
    hosts = ("inhire.app", "*.inhire.app")
    auto_submit_capable = False

    def __init__(self):
        self._last_modal_question = ""  # evita dupes entre ticks do watcher

    def watch_wait(self, page, ctx) -> None:
        """Modal de perguntas da empresa que aparece APOIS da diversidade/captcha.

        Escolha unica avanca sozinha ao marcar; multipla/texto precisa do botao
        Prosseguir — clicamos so quando ja respondemos algo neste tick.
        """
        modal = _question_modal(page)
        if modal is None:
            return
        question = _modal_question(modal)
        did = 0
        try:
            from ats_answers import answer_choice_groups

            answered, _deferred = answer_choice_groups(modal, ctx, log_prefix="inhire-modal")
            did += len(answered)
            for a in answered:
                LOG.info("InHire modal: %s", a)
        except Exception as exc:
            LOG.debug("InHire modal groups: %s", exc)
        try:
            did += _answer_modal_texts(modal, ctx)
        except Exception as exc:
            LOG.debug("InHire modal texts: %s", exc)
        if did and question and question != self._last_modal_question:
            self._last_modal_question = question
            # escolha UNICA avanca sozinha ao marcar (nada a clicar). Pergunta
            # multipla/escrita: clica 'Prosseguir/Continuar' se ainda houver modal.
            try:
                btns = modal.get_by_role("button", name=_MODAL_CONTINUE_RE).all()
                for btn in btns:
                    txt = (btn.inner_text(timeout=400) or "").strip()
                    if _MODAL_SUBMIT_RE.search(txt):
                        continue  # nunca finalizar/enviar
                    if btn.is_visible(timeout=400) and btn.is_enabled(timeout=400):
                        btn.click(timeout=4000)
                        break
            except Exception as exc:
                LOG.debug("InHire modal continue: %s", exc)
    success_regex = SUCCESS_RE

    @classmethod
    def can_handle(cls, url: str) -> bool:
        # mantém o comportamento legado: host OU substring em URLs estranhas
        return super().can_handle(url) or "inhire.app" in (url or "").casefold()

    def wait_ready(self, page, timeout_ms: int = 15000) -> bool:
        try:
            page.locator(NAME_FIELD_SELECTOR).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def detect_obstacles(self, page) -> list[str]:
        try:
            html = page.content()[:200000]
        except Exception:
            return ["pagina-inacessivel"]
        low = html.casefold()
        if any(marker in low for marker in CAPTCHA_MARKERS):
            return ["captcha"]
        return []

    def fill(self, page, ctx) -> FillResult:
        """Sequência manual comprovada em campo (dropdowns React, ordem do telefone).

        O kernel genérico não alcança os widgets custom do InHire; sobrescrever
        é o caso previsto pelo contrato. Nunca lança — devolve FillResult.
        """
        cfg = ctx.cfg
        resume_path = ctx.resume_path
        filled: list[str] = []
        missing: list[str] = []
        try:
            _prefer_english_ui(page)

            for label in ("Candidatar-se para a vaga", "Candidatar-se", "Candidatar", "Apply", "Apply for the job"):
                btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
                try:
                    if btn.count() and btn.first.is_visible(timeout=400):
                        btn.first.click(timeout=4000)
                        _pause(0.8, 1.4)
                        break
                except Exception:
                    continue

            if not self.wait_ready(page, timeout_ms=15000):
                return FillResult(ok=False, filled=filled, missing=["form"],
                                  error="Formulario InHire nao apareceu (campo nome).")

            name = (cfg.get("candidate_name") or "").strip()
            email = (cfg.get("candidate_email") or "").strip()
            phone = _local_phone_digits(cfg)
            city = (cfg.get("candidate_city") or "").strip() or "Belo Horizonte"
            city = city.split(",")[0].strip()
            linkedin = _linkedin_profile_value(cfg)
            cpf = format_cpf(cfg.get("candidate_cpf") or "")

            # Pretensão: moeda inferida do placeholder real do campo (R$ → BRL,
            # $ → USD). BRL ja vem com o multiplicador PJ aplicado por salary_for.
            salary = _salary_inhire(page, cfg, ctx)

            _fill_if_present(page, "#name, input[name='name']", name)
            filled.append("nome")
            # CPF — so se configurado; o campo mascarado aceita '123.456.789-01'.
            if cpf:
                _fill_if_present(
                    page, "input[name='document.value'], #cpf, input[name='cpf'], input[placeholder*='000.000']", cpf
                )
                filled.append("cpf")
            _fill_if_present(page, "#email, input[name='email']", email)
            filled.append("email")

            # Area code BEFORE the number.
            _select_phone_country_code(page, dial="+55")
            _fill_if_present(page, "#phone, input[name='phone']", phone)
            filled.append("telefone")

            _fill_if_present(page, "#linkedinUsername, input[name='linkedinUsername']", linkedin)
            filled.append("linkedin")

            # Country of origin — always type/select "Brazil" (English list).
            if _select_country_brazil(page):
                filled.append("pais")
            else:
                missing.append("pais")

            # City dropdown enables after country.
            _pause(0.5, 1.0)
            if _select_react_dropdown(
                page,
                matcher=r"Enter your city|Informe sua cidade|City|cidade",
                option_query=city,
                option_regex=re.escape(city),
            ):
                filled.append("cidade")
            else:
                # Only touch free-text city if enabled.
                try:
                    city_input = page.locator("#district, input[name='district'], input[name='districtBr']").first
                    if city_input.count() and city_input.is_enabled(timeout=800):
                        city_input.fill(city)
                        filled.append("cidade")
                    else:
                        missing.append("cidade")
                except Exception:
                    missing.append("cidade")

            if salary:
                _fill_if_present(page, "#salaryExpectation, input[name='salaryExpectation']", salary)
                filled.append("pretensao")

            if resume_path:
                try:
                    file_input = page.locator('input[type="file"][name="resume"], input[type="file"]')
                    if file_input.count():
                        file_input.first.set_input_files(resume_path)
                        _pause(0.6, 1.2)
                        filled.append("curriculo")
                except Exception as exc:
                    LOG.warning("InHire resume upload failed: %s", exc)
                    missing.append("curriculo")

            # Perguntas customizadas da empresa com opcoes nativas:
            # diversidade->perfil, empresa->IA+cache (o handler InHire sobrescreve
            # fill(), entao o kernel nao roda — chamamos a camada aqui direto).
            has_ai = bool((ctx.ai or {}).get("api_key"))
            groups_answered = False
            try:
                from ats_answers import answer_choice_groups

                extra, _deferred = answer_choice_groups(page, ctx, log_prefix="inhire")
                groups_answered = bool(extra)
                filled.extend(extra)
            except Exception as exc:
                LOG.debug("InHire choice groups: %s", exc)

            if not has_ai or not groups_answered:
                # Fallback legado (sem IA disponivel): hybrid Yes / referral No.
                # Com IA os radios ja foram respondidos com precisao — nao cobrir.
                _click_visible_choice(page, ("Yes", "Sim"), nth=0)
                _click_visible_choice(page, ("No", "Não", "Nao"), nth=1)

            _fill_diversity_step(page, cfg=cfg, ctx=ctx)
            filled.append("diversidade")

            # "Continuar registro" desabilitado = ficou pergunta obrigatoria sem
            # resposta (ex.: multi-select de grupos). Re-olhe ate 3x o que falta.
            for pass_no in range(1, 4):
                if not _continue_disabled(page):
                    break
                LOG.info("InHire: botao continuar desabilitado — re-olhando o que falta (passada %s)", pass_no)
                new_found = _retry_missing(page, ctx)
                if new_found:
                    filled.extend(new_found)
                    _pause(0.8, 1.5)
                elif pass_no > 1:
                    break  # duas passadas sem progresso
            # ainda travado = falta resposta obrigatoria que nao achamos: o
            # copiloto de IA assume (override total) em vez de ir pro humano.
            if _continue_disabled(page):
                missing.append("continuar-registro")
            return FillResult(ok=not missing, filled=filled, missing=missing)
        except Exception as exc:
            return FillResult(ok=False, filled=filled, missing=missing, error=str(exc))
