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

from ats_base import ApplyContext, FillResult, SubmitResult
from browser_engine import (
    click_scanned_button,
    find_first_button,
    scan_buttons,
    wait_for_matching_button,
)
from form_rules import find_rule_for_label, pick_select_option, resolve_rule_value

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
  const nodes = document.querySelectorAll('input, textarea, select');
  nodes.forEach((el, index) => {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'checkbox-hack'].includes(type)) return;
    if (type === 'checkbox' || type === 'radio') return; // handled por handler
    if (el.disabled) return;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (!rect.width || !rect.height || style.visibility === 'hidden') return;
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
    if (!label) label = [el.name, el.placeholder, el.id].filter(Boolean).join(' ');
    const options = el.tagName === 'SELECT' ? Array.from(el.options).map((o) => o.text) : [];
    el.setAttribute('data-radar-field', String(index));
    out.push({
      index,
      key: id || el.name || String(index),
      label: (label || '').slice(0, 400),
      tag: el.tagName.toLowerCase(),
      type,
      name: el.name || '',
      id,
      options,
      required: !!el.required,
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
    chosen = pick_select_option(options, preferred)
    if not chosen:
        return False
    try:
        page.locator(selector).first.select_option(label=chosen, timeout=4000)
        _pause(0.1, 0.3)
        return True
    except Exception:
        return False


def _apply_field(page, field: dict, rule: dict | None, ctx: ApplyContext) -> tuple[str, bool] | None:
    """Retorna (chave_preenchida, ok) ou None se não é para tocar no campo."""
    sel = field_selector(field)
    label = field.get("label") or field.get("key") or ""
    if rule is None:
        if field.get("required") and field.get("tag") in {"input", "textarea", "select"}:
            return (label, False)
        return None
    mode = str(rule.get("mode") or "text")
    if mode == "skip":
        return None
    value = resolve_rule_value(rule, ctx.cfg, cover_letter=ctx.cover_letter, resume_path=ctx.resume_path)
    if value is None:
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
    if field.get("tag") == "select" or mode == "select":
        preferred = str(value)
        if mode == "select" and rule.get("value"):
            preferred = str(rule["value"])
        if safe_select(page, sel, field.get("options") or [], preferred):
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
