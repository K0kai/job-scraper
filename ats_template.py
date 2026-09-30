"""TEMPLATE de handler ATS — copie este arquivo para ``ats_<site>.py``.

Guia completo com o passo a passo: ``docs/ats-handler-guide.md``.
Regras de ouro:

1. Seletores/extrâneos SEMPRE como constantes no topo, cada uma com
   ``# evidência: <URL real observada, data>`` — sem chute.
2. Nada de captcha/login automático: ``detect_obstacles`` apenas CLASSIFICA.
3. ``auto_submit_capable`` nasce False; só vira True após smoke assistido
   aprovado em candidatura real.
4. O ``@register`` aqui embaixo fica COMENTADO de propósito (o template não
   pode poluir o registry). Descomente ao renomear a classe.
"""
from __future__ import annotations

import re

from ats_base import BaseATSHandler

# from ats_base import register  # ← 4º passo: descomente junto com o @register

# ---------------------------------------------------------------------------
# Constantes do site (evidência obrigatória em cada uma)
# ---------------------------------------------------------------------------

# evidência: https://careers.exemplo.com/jobs/123 (2026-09-29, vistoria manual)
# BUTTON_APPLY_RE = re.compile(r"^apply for this job$", re.I)

# ---------------------------------------------------------------------------
# Sucesso (para verify_submitted / wait_for_human assistido)
# ---------------------------------------------------------------------------

# evidência: tela pós-envio real de https://careers.exemplo.com (2026-09-29)
# SUCCESS_RE = re.compile(r"thank you for applying|candidatura recebida", re.I)


class MeuAtsHandler(BaseATSHandler):
    # 1º passo: identificador único (logs/UI) — minúsculo, sem espaços.
    name = "meu_ats"

    # 2º passo: hosts do site. Sem "www." (o can_handle tolera).
    # Use "*.dominio.com" para subdomínios (ex.: Gupy *.gupy.io).
    hosts = ("careers.exemplo.com",)

    # 3º passo: leave False (assisted-first). True só após smoke real.
    auto_submit_capable = False

    # success_regex = SUCCESS_RE  # descomente quando definir

    # ------------------------------------------------------------------
    # Obrigatórios
    # ------------------------------------------------------------------

    def wait_ready(self, page, timeout_ms: int = 15000) -> bool:
        """O formulário apareceu? Poll curto e condition-based (SPAs atrasam).

        Default: o botão/âncora de "Apply" ou o primeiro input do form.
        """
        # Exemplo (adaptar): aguardar um campo característico do site.
        # return _wait_visible(page, "#candidate_name", timeout_ms)
        return True

    def detect_obstacles(self, page) -> list[str]:
        """Classifica etapas humanas. NUNCA resolve — só detecta.

        Exemplos de retorno: ["captcha"], ["login"], ["captcha", "login"].
        Vazio = robô pode seguir (e auto-enviar SE capable).
        """
        obstacles: list[str] = []
        try:
            html = page.content()[:200000]
        except Exception:
            return ["pagina-inacessivel"]
        if "recaptcha" in html or "hcaptcha" in html:
            obstacles.append("captcha")
        return obstacles

    # ------------------------------------------------------------------
    # Opcional: fill/submit têm default no kernel. Sobrescreva só se o
    # site foge do padrão (wizard custom, campos não-rotulados etc.).
    # ------------------------------------------------------------------

    # def fill(self, page, ctx):
    #     return super().fill(page, ctx)

    # ------------------------------------------------------------------
    # Hooks das manias do site (o kernel chama pre_fill/post_fill de graça)
    # ------------------------------------------------------------------

    def pre_fill(self, page, ctx) -> None:
        """Antes do kernel: abrir o formulário, trocar idioma PT/EN, aceitar cookies.

        Exemplo InHire: clicar 'Apply for the job' antes dos campos existirem.
        """

    def post_fill(self, page, ctx) -> list[str]:
        """Depois do kernel: widgets custom que o fill genérico não alcança.

        Exemplo (dropdown React custom — o kernel só conhece <select> nativo):

            # evidência: https://careers.exemplo.com (2026-09-29)
            dd = page.locator('[aria-label="Dropdown select"]').filter(has_text=...)
            dd.click(force=True); page.keyboard.type("Brazil"); ...
            return ["pais"]

        Retorne os nomes dos extras preenchidos (vão para FillResult.filled).
        """
        return []

    def advance_step(self, page) -> bool:
        """Wizard multi-step fora do padrão 'Next/Continue'.

        True = avançou (kernel repete o ciclo de campos no novo passo).
        False = kernel tenta o botão Next genérico.

        Exemplo Gupy: passos como <a> âncora com classe própria.
        """
        return False


# ---------------------------------------------------------------------------
# 4º passo (com renomeação da classe feita): descomente registro E adicione
# "ats_<site>" em _HANDLER_MODULES no ats_router.py.
# ---------------------------------------------------------------------------

# @register
# class MeuAtsHandler(BaseATSHandler):  ← renomeie a classe acima, nada aqui
#     pass
