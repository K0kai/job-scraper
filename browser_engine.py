"""Resolução do motor de navegador: Pydoll (padrão) ou Playwright (fallback).

Ambos expõem ``sync_playwright``; o Pydoll roda sobre CDP direto (sem
``navigator.webdriver``), o Playwright fica como fallback manual no painel.
"""
from __future__ import annotations

import importlib
import os
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
    real — mantém as reverificações de vagas funcionando quando um dos dois
    motores opcionais não estiver instalado.
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
