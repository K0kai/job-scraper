"""Greenhouse (boards.greenhouse.io / job-boards.greenhouse.io) — handler.

quirks (checklist guia): select nativo, upload resume nativo, checkbox de
consentimento "I agree" por vaga, sem captcha por padrão (mas pode haver).
Lançamento assisted-first: auto_submit_capable só vira True após smoke real.
"""
from __future__ import annotations

import re

from ats_base import BaseATSHandler, register

# evidência: docs públicos greenhouse.io + vistoria 2026-09-29; SMOKE pendente (guia passo 5)
# Antes do smoke, o que falhar aqui cai no modo assistido de forma segura.
CONSENT_CHECKBOX_RE = re.compile(
    r"agree|consent|privacy|terms|autorizo|concordo", re.I
)
CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha", "hcaptcha", "cf-challenge")
SUCCESS_RE = re.compile(
    r"thank you for your interest|application.{0,40}(has been )?sent|"
    r"we('|\u2019)?ve received|candidatura (enviada|recebida)",
    re.I,
)


@register
class GreenhouseHandler(BaseATSHandler):
    name = "greenhouse"
    hosts = ("boards.greenhouse.io", "job-boards.greenhouse.io")
    auto_submit_capable = False
    success_regex = SUCCESS_RE

    def wait_ready(self, page, timeout_ms: int = 15000) -> bool:
        try:
            page.wait_for_selector("form input, form textarea", timeout=timeout_ms)
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
        """Alguns boards abrem o form após clique em 'Apply for this job'."""
        try:
            btn = page.get_by_role("link", name=re.compile(r"apply for (this|the) job", re.I))
            if btn.count():
                btn.first.click(timeout=4000)
        except Exception:
            pass

    def post_fill(self, page, ctx) -> list[str]:
        """Eussem checkbox de consentimento/EULA obrigatória por vaga."""
        extras: list[str] = []
        try:
            boxes = page.locator("input[type='checkbox']")
            labels = page.locator("label")
            for i in range(boxes.count()):
                box = boxes.nth(i)
                try:
                    if box.is_checked():
                        continue
                    text = ""
                    for j in range(labels.count()):
                        lab = labels.nth(j)
                        try:
                            if lab.get_attribute("for") == (box.get_attribute("id") or "\u0000"):
                                text = lab.inner_text() or ""
                        except Exception:
                            continue
                    if CONSENT_CHECKBOX_RE.search(text):
                        box.check(force=True)
                        extras.append("consentimento")
                except Exception:
                    continue
        except Exception:
            pass
        return extras
