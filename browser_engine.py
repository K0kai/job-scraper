"""Resolução do motor de navegador: Pydoll (padrão) ou Playwright (fallback).

Ambos expõem ``sync_playwright``; o Pydoll roda sobre CDP direto (sem
``navigator.webdriver``), o Playwright fica como fallback manual no painel.
"""
from __future__ import annotations

import importlib
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