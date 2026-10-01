"""Resolução do motor de navegador: Pydoll (padrão) ou Playwright (fallback).

Ambos expõem ``sync_playwright``; o Pydoll roda sobre CDP direto (sem
``navigator.webdriver``), o Playwright fica como fallback manual no painel.
"""
from __future__ import annotations

import importlib
import os
import re
import time
from typing import Any

DEFAULT_ENGINE = "pydoll"
ENGINE_MODULES = {
    "pydoll": "pydoll.playwright.sync_api",
    "playwright": "playwright.sync_api",
}


def engine_name(cfg: dict[str, str] | None) -> str:
    name = (cfg or {}).get("browser_engine", DEFAULT_ENGINE)
    return name if name in ENGINE_MODULES else DEFAULT_ENGINE


def engine_module_name(cfg: dict[str, str] | None) -> str:
    return ENGINE_MODULES[engine_name(cfg)]


def resolve_sync_playwright(cfg: dict[str, str] | None) -> Any:
    """Retorna o módulo que expõe ``sync_playwright`` para o motor configurado.

    Se o motor escolhido (pydoll) não estiver instalado, cai para o Playwright
    real — nunca deixa o fluxo de candidatura sem motor.
    """
    wanted = engine_name(cfg)
    for candidate in (wanted, next(e for e in ENGINE_MODULES if e != wanted)):
        try:
            return importlib.import_module(ENGINE_MODULES[candidate])
        except ImportError:
            continue
    raise ImportError(
        "Nenhum motor de navegador disponível. Instale pydoll-python ou playwright."
    )


def engine_install_hint(cfg: dict[str, str] | None) -> str:
    name = engine_name(cfg)
    if name == "pydoll":
        return "pip install pydoll-python"
    return "pip install playwright && playwright install chromium"


# ---------------------------------------------------------------------------
# Args de launch (Chrome exige --no-sandbox quando roda como root, ex.: container)
# ---------------------------------------------------------------------------

STEALTH_ARG = "--disable-blink-features=AutomationControlled"

# Windows (e outros) throttlam timers/JS em janelas sem foco; LinkedIn SPA para.
# Usado na reverificação para rodar atrás de outras janelas sem “travar”.
BACKGROUND_RUN_ARGS = (
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def chrome_launch_args(
    *,
    root_no_sandbox: bool = True,
    extra: list[str] | None = None,
) -> list[str]:
    args = [STEALTH_ARG]
    if root_no_sandbox and _is_root():
        args.append("--no-sandbox")
    for arg in extra or []:
        if arg not in args:
            args.append(arg)
    return args


def persistent_launch_kwargs(
    profile: str,
    *,
    headless: bool = False,
    slow_mo: int | None = None,
    viewport: dict[str, int] | None = None,
    locale: str | None = None,
    args: list[str] | None = None,
    ignore_default_args: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """kwargs únicos para ``launch_persistent_context`` nos dois motores."""
    kwargs: dict[str, Any] = {
        "user_data_dir": profile,
        "headless": headless,
        "args": chrome_launch_args(extra=args),
        "ignore_default_args": list(ignore_default_args or ["--enable-automation"]),
    }
    if slow_mo is not None:
        kwargs["slow_mo"] = slow_mo
    if viewport is not None:
        kwargs["viewport"] = viewport
    if locale is not None:
        kwargs["locale"] = locale
    kwargs.update(extra)
    return kwargs


# ---------------------------------------------------------------------------
# Varredura rápida de botões (1 evaluate em JS vs N round-trips CDP por .nth())
# ---------------------------------------------------------------------------

_BUTTON_SCAN_JS = """() => {
  const out = [];
  const nodes = document.querySelectorAll('button, a[href]');
  nodes.forEach((el, index) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const visible = rect.width > 0 && rect.height > 0
      && style.visibility !== 'hidden' && style.display !== 'none';
    out.push({
      index,
      text: (el.innerText || '').trim().slice(0, 200),
      aria: (el.getAttribute('aria-label') || '').trim(),
      visible,
      tag: el.tagName.toLowerCase() === 'a' ? 'a[href]' : 'button',
    });
  });
  return out;
}"""

MARK_ATTR = "data-radar-click"


def scan_buttons(page) -> list[dict[str, Any]]:
    """Todos os botões/links da página em um único ``page.evaluate``."""
    try:
        return list(page.evaluate(_BUTTON_SCAN_JS) or [])
    except Exception:
        return []


def _button_text(item: dict[str, Any]) -> str:
    return f"{item.get('text') or ''} {item.get('aria') or ''}".strip()


def find_first_button(
    buttons: list[dict[str, Any]],
    patterns: list[re.Pattern[str]],
    *,
    exclude: list[re.Pattern[str]] | None = None,
    tag: str | None = None,
) -> int | None:
    """Índice do primeiro botão visível que casa ``patterns`` (sem casar ``exclude``)."""
    for item in buttons:
        if not item.get("visible"):
            continue
        if tag and item.get("tag") != tag:
            continue
        hay = _button_text(item)
        if not hay:
            continue
        if not any(p.search(hay) for p in patterns):
            continue
        if exclude and any(p.search(hay) for p in exclude):
            continue
        return int(item["index"])
    return None


def _poll_sleep(seconds: float) -> None:
    time.sleep(seconds)


def wait_for_matching_button(
    page,
    patterns: list[re.Pattern[str]],
    *,
    timeout_ms: float = 8000,
    interval_s: float = 0.4,
    exclude: list[re.Pattern[str]] | None = None,
    tag: str | None = None,
) -> int | None:
    """Polling condition-based (SPAs renderizam o botão após o DOMContentLoaded)."""
    deadline = time.monotonic() + max(0.0, timeout_ms) / 1000.0
    while True:
        idx = find_first_button(
            scan_buttons(page), patterns, exclude=exclude, tag=tag
        )
        if idx is not None:
            return idx
        if time.monotonic() >= deadline:
            return None
        _poll_sleep(interval_s)


def click_scanned_button(page, index: int) -> None:
    """Marca o índice da varredura com atributo e clica via locator — evita drift
    de ``.nth()`` quando o DOM muda entre scan e clique."""
    page.evaluate(
        """(index) => {
          document.querySelectorAll('[data-radar-click]').forEach((el) => el.removeAttribute('data-radar-click'));
          const nodes = document.querySelectorAll('button, a[href]');
          if (nodes[index]) nodes[index].setAttribute('data-radar-click', '1');
        }""",
        index,
    )
    try:
        page.locator(f'[{MARK_ATTR}="1"]').first.click(timeout=5000)
    finally:
        try:
            page.evaluate(
                """() => document.querySelectorAll('[data-radar-click]').forEach((el) => el.removeAttribute('data-radar-click'))"""
            )
        except Exception:
            pass