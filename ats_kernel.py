"""Kernel genérico de preenchimento de formulários ATS + salvaguardas de foco.

Todo handler usa isto por default; sobrescrever só as manias do site nos hooks.
As salvaguardas anti-popup (re-âncora, fechar intrusos, recuperar elemento
detached) são **obrigatórias** e não substituíveis — foi o adendo aprovado no
design: o fill deve se atentar a desfoque de página por popup.

Nunca lança exceção para o roteador: tudo vira ``FillResult``/``SubmitResult``.
"""
from __future__ import annotations

import logging
import random
import re
import time

from ats_answers import (
    DIVERSITY_RE,
    _infer_category_from_options,
    answer_choice_groups,
    classify_group,
    diversity_answer_for_options,
)
from ats_base import ApplyContext, FillResult, SubmitResult
from browser_engine import (
    click_scanned_button,
    find_first_button,
    scan_buttons,
    wait_for_matching_button,
)
from form_rules import (
    extract_current_employer,
    find_rule_for_label,
    infer_candidate_country,
    job_context_text,
    pick_select_option,
    prepare_text_value,
    resolve_rule_value,
)

LOG = logging.getLogger("job-scraper")

MAX_STEPS = 8  # wizards longos sem fim = bug, não paciência infinita
MARK_FIELD_ATTR = "data-radar-field"

# ---------------------------------------------------------------------------
# Listas de padrões (compartilhadas por kernel e handlers)
# ---------------------------------------------------------------------------

#: Submit de ATS em geral. Excluído "Easy Apply" (LinkedIn) pela blacklist.
SUBMIT_PATTERNS = [
    re.compile(r"submit\s+application|submit\s+your\s+application", re.I),
    re.compile(r"^submit$", re.I),
    re.compile(r"enviar\s+(a\s+)?candidatura|finalizar\s+(a\s+)?candidatura", re.I),
    re.compile(r"complete\s+application|send\s+application", re.I),
]
#: Nunca clicados por varreduras de conveniência (intrusos, wizard next).
SUBMIT_LABEL_BLACKLIST = re.compile(r"submit|apply|enviar|candidatar|finalizar|complete", re.I)
EASY_APPLY_EXCLUDE = [re.compile(r"easy\s*apply", re.I)]
NEXT_PATTERNS = [
    re.compile(r"^(next|continue|avançar|avancar|próximo|proximo|seguinte)\b", re.I),
]

# ---------------------------------------------------------------------------
# JavaScripts de varredura (1 evaluate por rodada — sem N round-trips CDP)
# ---------------------------------------------------------------------------

#: Coleta campos rotulados (mesma heurística de _collect_controls do LinkedIn)
#: e já os marca com data-radar-field para locator estável contra re-render.
_FIELDS_JS = """() => {
  const out = [];
  let index = 0;
  const push = (el, meta) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (!rect.width || !rect.height || style.visibility === 'hidden' || style.display === 'none') return;
    el.setAttribute('data-radar-field', String(index));
    out.push(Object.assign({ index }, meta));
    index += 1;
  };
  const labelFor = (el) => {
    const id = el.id || '';
    let label = '';
    if (id) {
      const lab = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (lab) label = lab.innerText || '';
    }
    if (!label && el.closest('label')) label = el.closest('label').innerText || '';
    if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');
    if (!label) {
      const legend = el.closest('fieldset')?.querySelector('legend');
      if (legend) label = legend.innerText || '';
    }
    if (!label) {
      const prev = el.previousElementSibling;
      if (prev && /label|span|p|div/i.test(prev.tagName)) label = prev.innerText || '';
    }
    if (!label) label = [el.name, el.placeholder, el.id, el.getAttribute('aria-label')].filter(Boolean).join(' ');
    return (label || '').slice(0, 400);
  };
  document.querySelectorAll('input, textarea, select').forEach((el) => {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'checkbox-hack'].includes(type)) return;
    if (type === 'checkbox' || type === 'radio') return;
    if (el.disabled) return;
    const options = el.tagName === 'SELECT' ? Array.from(el.options).map((o) => o.text) : [];
    push(el, {
      key: el.id || el.name || String(index),
      label: labelFor(el),
      tag: el.tagName.toLowerCase(),
      type,
      name: el.name || '',
      id: el.id || '',
      placeholder: el.placeholder || '',
      options,
      required: !!el.required,
      widget: el.tagName === 'SELECT' ? 'select' : 'input',
    });
  });
  document.querySelectorAll(
    '[aria-label="Dropdown select"], [role="combobox"], [class*="react-dropdown-select"]'
  ).forEach((el) => {
    if (el.closest('[data-radar-field]')) return;
    if (el.querySelector('[data-radar-field]')) return;
    const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
    push(el, {
      key: el.id || el.getAttribute('aria-label') || String(index),
      label: (text || labelFor(el) || 'dropdown').slice(0, 400),
      tag: 'combobox',
      type: 'combobox',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: '',
      options: [],
      required: false,
      widget: 'combobox',
    });
  });
  return out;
}"""

#: Fecha overlays/intrusos. A lista Negra SUBMIT impede clicar envio por engano
#: quando um botão de fechar share textão com o de enviar (formulários ruins).
INTRUDER_JS = """() => {
  const SUBMIT = /submit|apply|enviar|candidatar|finalizar|complete/i;
  const CLOSE = /^(aceitar(\\s+tudo)?|accept(\\s+all)?|agree|i\\s+agree|concordo|fechar|close|ok|entendi|dismiss)$/i;
  const clicked = [];
  for (const el of document.querySelectorAll('button, [role="button"], a')) {
    const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
    if (!text) continue;
    if (SUBMIT.test(text)) continue;        // nunca clicar envio nesta varredura
    if (!CLOSE.test(text)) continue;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) continue;
    try { el.click(); clicked.push(text); } catch (e) {}
  }
  return clicked;
}"""


def _pause(lo: float = 0.2, hi: float = 0.6) -> None:
    time.sleep(random.uniform(lo, hi))


# ---------------------------------------------------------------------------
# Peças do pipeline
# ---------------------------------------------------------------------------


def collect_fields(page, *, strict: bool = False) -> list[dict]:
    """Campos visíveis + labels, já marcados com ``data-radar-field``."""
    try:
        raw = page.evaluate(_FIELDS_JS)
    except Exception:
        if strict:
            raise
        return []
    return list(raw or [])


def dismiss_intruders(page) -> list[str]:
    """Fecha cookie-banner/overlay de chat. Retorna rótulos clicados."""
    try:
        return list(page.evaluate(INTRUDER_JS) or [])
    except Exception:
        return []


def field_selector(field: dict) -> str:
    return f'[{MARK_FIELD_ATTR}="{field["index"]}"]'


def safe_fill(page, selector: str, value: str) -> bool:
    """scroll→click→fill; se o elemento sumiu (popup abriu no meio), fecha
    intrusos, relocaliza e tenta de novo. Segunda falha = False (→ missing)."""
    try:
        loc = page.locator(selector).first
        loc.scroll_into_view_if_needed(timeout=2000)
        loc.click(timeout=3000)
        loc.fill(value, timeout=5000)
        return True
    except Exception:
        pass
    dismiss_intruders(page)
    _pause(0.15, 0.35)
    try:
        loc = page.locator(selector).first
        loc.scroll_into_view_if_needed(timeout=2000)
        loc.fill(value, timeout=5000)
        return True
    except Exception as exc:
        LOG.debug("safe_fill(%s) falhou 2x: %s", selector, exc)
        return False


def safe_select(page, selector: str, options: list[str], preferred: str) -> bool:
    chosen = pick_select_option(options, preferred) if options else None
    if not chosen:
        chosen = (preferred or "").strip() or None
    if not chosen:
        return False
    try:
        page.locator(selector).first.select_option(label=chosen, timeout=4000)
        _pause(0.1, 0.3)
        return True
    except Exception:
        pass
    # Combobox / react-select: type-to-filter + option click.
    return safe_combo_select(page, selector, chosen)


def safe_combo_select(page, selector: str, preferred: str) -> bool:
    """Abre dropdown custom (React/ARIA), digita filtro e escolhe a opção."""
    query = (preferred or "").strip()
    if not query:
        return False
    try:
        loc = page.locator(selector).first
        loc.scroll_into_view_if_needed(timeout=3000)
        loc.click(timeout=5000, force=True)
        _pause(0.2, 0.45)
        page.keyboard.type(query, delay=random.randint(20, 45))
        _pause(0.45, 0.9)
        opt_re = re.compile(re.escape(query.split("(")[0].strip()) or query, re.I)
        opt = page.locator(
            "[role='option'], "
            ".react-dropdown-select-dropdown button, "
            ".react-dropdown-select-dropdown [role='option'], "
            "[role='listbox'] [role='option']"
        ).filter(has_text=opt_re)
        if opt.count():
            opt.first.click(timeout=5000, force=True)
        else:
            page.keyboard.press("Enter")
        _pause(0.35, 0.7)
        return True
    except Exception as exc:
        LOG.debug("safe_combo_select(%s) falhou: %s", selector, exc)
        return False


def _apply_field(page, field: dict, rule: dict | None, ctx: ApplyContext) -> tuple[str, bool] | None:
    """Retorna (chave_preenchida, ok) ou None se não é para tocar no campo."""
    sel = field_selector(field)
    label = field.get("label") or field.get("key") or ""
    if rule is None:
        # pergunta de diversidade em <select> nativo → perfil do painel (nunca IA)
        if field.get("tag") == "select" and DIVERSITY_RE.search(label):
            kind = classify_group(label)
            category = kind.split(":", 1)[1]
            if category == "other":
                category = _infer_category_from_options(field.get("options") or []) or "other"
            got = diversity_answer_for_options(ctx.cfg, category, field.get("options") or [])
            if got and safe_select(page, sel, field.get("options") or [], got):
                return (f"diversidade:{category}", True)
            return None  # sem perfil/opção: deixa para o humano, nao é erro
        if field.get("required") and field.get("tag") in {"input", "textarea", "select"}:
            return (label, False)
        return None
    mode = str(rule.get("mode") or "text")
    if mode == "skip":
        return None
    field_hint = " ".join(
        str(field.get(k) or "") for k in ("label", "placeholder", "name", "id")
    )
    resume_json = None
    if isinstance(ctx.ai, dict):
        resume_json = ctx.ai.get("resume_json")
    value = resolve_rule_value(
        rule, ctx.cfg, cover_letter=ctx.cover_letter, resume_path=ctx.resume_path,
        field_hint=field_hint,
        job_text=job_context_text((ctx.ai or {}).get("job")),
        resume_json=resume_json,
    )
    if value is None:
        return None
    # "ask"/"both" do painel = decisao humana (ex.: Contractor × Employee)
    if str(value).strip().casefold() in {"ask", "both", "nao_informado", "not_informed"}:
        return None
    if not str(value).strip():
        return (str(rule.get("key") or label), False)
    key = str(rule.get("key") or label)
    if mode == "file" or field.get("type") == "file":
        try:
            page.locator(sel).first.set_input_files(str(value), timeout=8000)
            _pause(0.4, 0.8)
            return (key, True)
        except Exception as exc:
            LOG.debug("upload falhou (%s): %s", sel, exc)
            return (key, False)
    value = prepare_text_value(rule, str(value), field_hint)
    is_combo = (
        str(field.get("widget") or "").casefold() == "combobox"
        or str(field.get("tag") or "").casefold() == "combobox"
        or str(field.get("type") or "").casefold() == "combobox"
    )
    if mode == "salary":
        from salary_review import finalize_salary_value

        value = finalize_salary_value(
            ctx.cfg,
            field_hint=field_hint,
            job_text=job_context_text((ctx.ai or {}).get("job")),
            options=list(field.get("options") or []),
            ai=ctx.ai if isinstance(ctx.ai, dict) else None,
            fallback=str(value),
        ) or str(value)
        # input de texto: digita no formato da moeda inferida. select nativo
        # (faixas): tenta casar; sem opcao compativel → humano decide.
        if field.get("tag") == "select" or is_combo:
            preferred = str(rule.get("value") or "").strip() or str(value)
            if safe_select(page, sel, field.get("options") or [], preferred):
                return (key, True)
            if str(value).strip() and safe_select(page, sel, field.get("options") or [], str(value)):
                return (key, True)
            return (key, False)
        if safe_fill(page, sel, str(value)):
            return (key, True)
        return (key, False)
    if field.get("tag") == "select" or mode == "select" or is_combo:
        preferred = str(value).strip()
        # rule.value só como fallback se o valor resolvido veio vazio
        if not preferred and mode == "select" and rule.get("value"):
            preferred = str(rule["value"]).strip()
        if not preferred:
            return (key, False)
        if safe_select(page, sel, field.get("options") or [], preferred):
            return (key, True)
        if preferred != str(value).strip() and str(value).strip():
            if safe_select(page, sel, field.get("options") or [], str(value)):
                return (key, True)
        return (key, False)
    if safe_fill(page, sel, str(value)):
        return (key, True)
    return (key, False)


def fill_page(page, ctx: ApplyContext, handler=None) -> FillResult:
    """Pipeline completo: hooks → campos por regra → wizard com guarda de
    obrigatórios → post_fill do handler. Nunca levanta exceção."""
    filled: list[str] = []
    missing: list[str] = []
    try:
        if handler is not None:
            handler.pre_fill(page, ctx)
        for _step in range(MAX_STEPS):
            fields = collect_fields(page, strict=True)
            for field in fields:
                rule = find_rule_for_label(field.get("label", ""), ctx.rules)
                outcome = _apply_field(page, field, rule, ctx)
                if outcome is None:
                    continue
                key, ok = outcome
                if ok:
                    filled.append(key)
                else:
                    missing.append(key)
            if missing:
                # Passo travado: não avança wizard com obrigatório faltando.
                break
            advanced = False
            if handler is not None:
                try:
                    advanced = bool(handler.advance_step(page))
                except Exception:
                    advanced = False
            if not advanced:
                if not fields:
                    break
                idx = wait_for_matching_button(
                    page, NEXT_PATTERNS, timeout_ms=1500, interval_s=0.3
                )
                if idx is None:
                    break
                try:
                    click_scanned_button(page, idx)
                except Exception:
                    break
                _pause(0.8, 1.4)
        if handler is not None:
            try:
                filled.extend(handler.post_fill(page, ctx) or [])
            except Exception as exc:
                LOG.debug("post_fill falhou: %s", exc)
        # perguntas da empresa c/ opcoes (radio/checkbox): diversidade→perfil,
        # demais→IA com cache global; insegura → fica com o humano.
        try:
            answered, _deferred = answer_choice_groups(page, ctx, log_prefix="kernel")
            filled.extend(answered)
        except Exception as exc:
            LOG.debug("grupos de opcoes falharam: %s", exc)
        return FillResult(ok=not missing, filled=filled, missing=missing)
    except Exception as exc:
        return FillResult(
            ok=False,
            filled=filled,
            missing=missing,
            error=str(exc) or "pagina fechada ou inacessivel",
        )


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------


def find_submit_button(page, *, scanned: list[dict] | None = None) -> int | None:
    buttons = scanned if scanned is not None else scan_buttons(page)
    return find_first_button(buttons, SUBMIT_PATTERNS, exclude=EASY_APPLY_EXCLUDE)


def click_submit(page) -> SubmitResult:
    """Localiza e clica o submit real do ATS (3 tentativas com polling curto)."""
    for _ in range(3):
        buttons = scan_buttons(page)
        idx = find_submit_button(page, scanned=buttons)
        if idx is not None:
            label = ""
            for b in buttons:
                if b.get("index") == idx:
                    label = (b.get("text") or b.get("aria") or "")[:80]
                    break
            try:
                click_scanned_button(page, idx)
                return SubmitResult(clicked=True, button_label=label)
            except Exception as exc:
                return SubmitResult(clicked=False, button_label=label, error=str(exc))
        _pause(0.6, 1.2)
    return SubmitResult(clicked=False, error="botao de envio nao encontrado")
