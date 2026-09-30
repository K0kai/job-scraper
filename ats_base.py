"""Contrato dos handlers de ATS + registry.

Cada página de candidatura externa (InHire, Greenhouse, Lever, Gupy...) vira uma
classe ``BaseATSHandler`` registrada com ``@register``. O ``ats_router`` pergunta
``find_handler(url)`` e decide auto-submit × assistido × NO_HANDLER.

Para criar um handler novo, copie ``ats_template.py`` e siga
``docs/ats-handler-guide.md``.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Tipos de dados que atravessam o fluxo
# ---------------------------------------------------------------------------


@dataclass
class ApplyContext:
    cfg: dict[str, str]
    rules: list[dict[str, Any]]
    resume_path: str
    cover_letter: str
    salary: str = ""
    #: contexto p/ perguntas de empresa via IA (job, resume_summary, resume_json,
    #: facts, provider, model, api_key, connect_fn, now_iso). Vazio = sem IA.
    ai: dict[str, Any] = field(default_factory=dict)


@dataclass
class FillResult:
    ok: bool
    filled: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class SubmitResult:
    clicked: bool
    button_label: str = ""
    error: str | None = None


# ---------------------------------------------------------------------------
# Classe base
# ---------------------------------------------------------------------------


class BaseATSHandler(ABC):
    """Interface única de um driver de ATS.

    Obrigatório por site: ``name``, ``hosts``, ``wait_ready``, ``detect_obstacles``.
    O default de ``fill``/``submit`` delega ao kernel genérico (``ats_kernel``);
    sobrescreva apenas as manias do site nos hooks ``pre_fill``/``post_fill``/
    ``advance_step``.
    """

    name: str = ""
    hosts: tuple[str, ...] = ()
    #: sites com captcha/etapa humana nascem False; vira True só após smoke real.
    auto_submit_capable: bool = False
    #: regex de "deu certo" para verify_submitted/wait_for_human (cada site define).
    success_regex: re.Pattern[str] | None = None

    # -- identificação ------------------------------------------------------

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Casa o netloc da URL (±www, ±porta) contra ``hosts``; ``*.dom`` = subdomínios."""
        try:
            host = urlparse(url or "").netloc.casefold()
        except Exception:
            return False
        if not host:
            return False
        host = host.rsplit(":", 1)[0] if ":" in host else host
        bare = host[4:] if host.startswith("www.") else host
        for pattern in cls.hosts:
            p = pattern.casefold().strip()
            if p.startswith("*."):
                suffix = p[2:]
                if bare == suffix or bare.endswith("." + suffix):
                    return True
            elif bare == p:
                return True
        return False

    # -- ciclo de vida do preenchimento --------------------------------------

    @abstractmethod
    def wait_ready(self, page, timeout_ms: int = 15000) -> bool:
        """A página/form chegou? (polling; False aborta o fill)."""

    @abstractmethod
    def detect_obstacles(self, page) -> list[str]:
        """Obstáculos humanos: ex. ``["captcha"]``, ``["login"]``. Vazio = automatizável."""

    def fill(self, page, ctx: ApplyContext) -> FillResult:
        """Default: kernel genérico com os hooks desta classe."""
        from ats_kernel import fill_page

        return fill_page(page, ctx, handler=self)

    def submit(self, page) -> SubmitResult:
        from ats_kernel import click_submit

        return click_submit(page)

    def verify_submitted(self, page) -> str:
        """``"submitted"`` | ``"unknown"`` — default usa ``success_regex`` no body."""
        if self.success_regex is None:
            return "unknown"
        try:
            text = (page.inner_text("body") or "")[:8000]
        except Exception:
            return "unknown"
        return "submitted" if self.success_regex.search(text) else "unknown"

    # -- hooks por site (default = no-op) ------------------------------------

    def pre_fill(self, page, ctx: ApplyContext) -> None:
        """Antes do kernel: trocar idioma, abrir formulário, etc."""

    def post_fill(self, page, ctx: ApplyContext) -> list[str]:
        """Depois do kernel: widgets custom (dropdowns React etc.). Retorna extras preenchidos."""
        return []

    def advance_step(self, page) -> bool:
        """Avanço de wizard não-padrão; False → kernel usa o botão Next genérico."""
        return False

    def watch_wait(self, page, ctx: ApplyContext) -> None:
        """Durante a janela assistida, chamado a cada ~4s (via on_tick).

        Use para widgets que aparecem DEPOIS do preenchimento/captcha — ex.:
        modal de perguntas da empresa. Nunca deve lançar (router protege, mas
        mantenha o corpo defensivo).
        """
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

HANDLERS: list[type[BaseATSHandler]] = []


def register(cls: type[BaseATSHandler]) -> type[BaseATSHandler]:
    """Decora a classe do handler e a adiciona ao registry.

    Falha dura no import se outro handler reivindicar o mesmo ``name`` ou um host
    já registrado — colisão de dispatch é pior que erro de import.
    """
    if not cls.name or not cls.hosts:
        raise RuntimeError(f"handler {cls.__name__}: name/hosts obrigatórios")
    taken_names = {h.name for h in HANDLERS}
    if cls.name in taken_names:
        raise RuntimeError(f"handler duplicado name={cls.name!r}")
    taken_hosts = {h.casefold() for other in HANDLERS for h in other.hosts}
    clash = taken_hosts & {h.casefold() for h in cls.hosts}
    if clash:
        raise RuntimeError(f"handler {cls.name}: hosts já reivindicados {sorted(clash)}")
    HANDLERS.append(cls)
    return cls


def find_handler(url: str) -> type[BaseATSHandler] | None:
    for cls in HANDLERS:
        try:
            if cls.can_handle(url):
                return cls
        except Exception:
            continue
    return None


__all__ = [
    "ApplyContext",
    "BaseATSHandler",
    "FillResult",
    "HANDLERS",
    "SubmitResult",
    "find_handler",
    "register",
]
