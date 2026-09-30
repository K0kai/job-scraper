"""Lever (jobs.lever.co / apply.lever.co) — handler.

quirks (checklist guia): form de página única com campos nativos, mas com
fieldset "Tell us about your background", consentimento de privacidade
obrigatório, e resume via upload nativo. Captcha raro, mas existe.
Lançamento assisted-first.
"""
from __future__ import annotations

import re

from ats_base import BaseATSHandler, register

# evidência: docs Lever + vistoria 2026-09-29; SMOKE pendente (guia passo 5)
PRIVACY_CONSENT_RE = re.compile(r"consent|privacy|terms|autorizo|concordo", re.I)
CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha", "hcaptcha")
SUCCESS_RE = re.compile(
    r"thank you for applying|application (has been )?(sent|received)|"
    r"you're all set|we('|\u2019)ll be in touch",
    re.I,
)


@register
class LeverHandler(BaseATSHandler):
    name = "lever"
    hosts = ("jobs.lever.co", "apply.lever.co")
    auto_submit_capable = False
    success_regex = SUCCESS_RE

    def wait_ready(self, page, timeout_ms: int = 15000) -> bool:
        try:
            page.wait_for_selector("#apply-form, form.application-form", timeout=timeout_ms)
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

    def pre_fill(self, page, ctx) -> None:
        try:
            btn = page.get_by_role("button", name=re.compile(r"apply", re.I))
            if btn.count():
                btn.first.click(timeout=4000)
        except Exception:
            pass

    def post_fill(self, page, ctx) -> list[str]:
        extras: list[str] = []
        # consentimento de privacidade (checkbox ou radio "I consent")
        try:
            consent = page.locator(
                "label, span, div"
            ).filter(has_text=PRIVACY_CONSENT_RE)
            for i in range(min(consent.count(), 10)):
                try:
                    node = consent.nth(i)
                    box = node.locator("input[type='checkbox']").first
                    if box.count() and not box.is_checked():
                        box.check(force=True)
                        extras.append("privacidade")
                except Exception:
                    continue
        except Exception:
            pass
        return extras
