"""Hybrid captcha (pydoll): clicar Turnstile como humano, antes de chamar você.

O motor pydoll expoe `page.mouse.click(x, y, humanize=True)` (curvas de Bezier)
e o shim sync o preserva. Turnstile decide por trust score — o clique humanizado
no host do widget e exatamente o que a doc do pydoll recomenda ("automate the
click, not defeat the captcha").

Limites honestos (doc pydoll): NAO resolve reCAPTCHA/hCaptcha nem puzzles de
imagem. InHire usa reCAPTCHA -> la continua humano; LinkedIn/Turnstile -> aqui.
Sem evidence estrutural de Turnstile, nao clicamos nada.
"""
from __future__ import annotations

import logging
import random
import time

LOG = logging.getLogger("job-scraper")

_TURNSTILE_HOST_JS = """() => {
  const sel = 'div.cf-turnstile, form .cf-turnstile > div, [data-sitekey], iframe[src*="challenges.cloudflare.com"]';
  const host = document.querySelector(sel);
  if (!host) return null;
  const r = host.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  return { x: r.x, y: r.y, w: r.width, h: r.height };
}"""

_TURNSTILE_MARKER_JS = """() => {
  const html = document.documentElement.outerHTML;
  return /challenges\\.cloudflare\\.com|cf-turnstile/i.test(html)
    && !/cf-chl-widget.*\\[done\\]/i.test(html);
}"""

_TURNSTILE_Solved_JS = """() => {
  const host = document.querySelector('div.cf-turnstile, [data-sitekey]');
  if (!host) return true;
  const inp = host.querySelector('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]')
    || document.querySelector('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]');
  return !!(inp && (inp.value || '').length > 8);
}"""


def _human_click(page, x: float, y: float) -> bool:
    """Click humanizado se o motor suportar (pydoll); senao click comum."""
    mouse = getattr(page, "mouse", None)
    if mouse is None:
        return False
    try:
        try:
            mouse.move(x, y, humanize=True)
        except TypeError:  # motor sem humanize (playwright) — movimento simples
            mouse.move(x, y)
        time.sleep(random.uniform(0.15, 0.5))
        try:
            mouse.click(x, y, humanize=True)
        except TypeError:
            mouse.click(x, y)
        return True
    except Exception as exc:
        LOG.debug("click humanizado falhou: %s", exc)
        return False


def has_turnstile(page) -> bool:
    try:
        return bool(page.evaluate(_TURNSTILE_MARKER_JS))
    except Exception:
        return False


def try_solve_turnstile(page, *, attempts: int = 2, wait_s: float = 6.0) -> bool:
    """Tenta resolver um Cloudflare Turnstile via clique humanizado. Retorna True
    se o token de resposta apareceu. Best-effort: nunca levanta."""
    if not has_turnstile(page):
        return False
    for attempt in range(1, attempts + 1):
        try:
            box = page.evaluate(_TURNSTILE_HOST_JS)
        except Exception:
            return False
        if not box:
            return False
        # checkbox fica ~30px a dentro do host, centrado na vertical
        x = box["x"] + 30 + random.uniform(-3, 3)
        y = box["y"] + box["h"] / 2 + random.uniform(-2, 2)
        LOG.info("turnstile hybrid: tentativa %s clicando (%s, %s)", attempt, int(x), int(y))
        if not _human_click(page, x, y):
            return False
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                if page.evaluate(_TURNSTILE_Solved_JS):
                    LOG.info("turnstile hybrid: resolvido (tentativa %s)", attempt)
                    return True
                if not has_turnstile(page):
                    return True  # widget sumiu (pagina avancou)
            except Exception:
                return True  # navegou com o clique — sinal de sucesso
    LOG.info("turnstile hybrid: nao resolvido; deixa para o humano")
    return False
