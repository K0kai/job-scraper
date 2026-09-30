"""Gupy (portal.gupy.io / *.gupy.io) — handler.

quirks (checklist guia): exige conta Gupy (login) na maioria das vagas — por
isso detect_obstacles reporta "login" e o fluxo fica assistido; telefone BR com
máquina; perguntas de diversidade obrigatórias; submit "Finalizar candidatura".
UI em PT-BR. Lançamento assisted-first (e o login mantém assistido de qualquer forma).
"""
from __future__ import annotations

import re

from ats_base import BaseATSHandler, register

# evidência: portal.gupy.io/job-application/* (vistoria 2026-09-29); SMOKE pendente
LOGIN_WALL_MARKERS = ("entre com sua conta", "faça login", "login-required", "entrar com")
CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha", "hcaptcha")
SUCCESS_RE = re.compile(
    r"candidatura (enviada|realizada com sucesso)|processo seletivo em andamento|"
    r"em breve retornaremos",
    re.I,
)


@register
class GupyHandler(BaseATSHandler):
    name = "gupy"
    hosts = ("portal.gupy.io", "*.gupy.io")
    auto_submit_capable = False
    success_regex = SUCCESS_RE

    def wait_ready(self, page, timeout_ms: int = 15000) -> bool:
        try:
            page.wait_for_selector("form input, [class*='form']", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def detect_obstacles(self, page) -> list[str]:
        try:
            html = page.content()[:200000]
        except Exception:
            return ["pagina-inacessivel"]
        low = html.casefold()
        obstacles: list[str] = []
        if any(marker in low for marker in LOGIN_WALL_MARKERS):
            obstacles.append("login")
        if any(marker in low for marker in CAPTCHA_MARKERS):
            obstacles.append("captcha")
        return obstacles

    def pre_fill(self, page, ctx) -> None:
        # Gupy abre o form atrás de "Candidate-se"/"Apply".
        try:
            btn = page.get_by_role("button", name=re.compile(r"candidate-se|apply", re.I))
            if btn.count():
                btn.first.click(timeout=4000)
        except Exception:
            pass

    def post_fill(self, page, ctx) -> list[str]:
        """Máscara de telefone BR: muitos campos Gupy esperam (DD) NNNNN-NNNN digitado
        como texto simples e validam no blur — o kernel já encheu; aqui só confirmamos
        aceitando eventuais diálogos de 'continuar sem login' que travam o form."""
        extras: list[str] = []
        try:
            keep = page.get_by_role("button", name=re.compile(r"continuar sem|skip", re.I))
            if keep.count():
                keep.first.click(timeout=3000)
                extras.append("skip-login")
        except Exception:
            pass
        return extras
