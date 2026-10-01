"""Modo de autonomia da candidatura: review (humano envia) × auto (robô envia)."""
from __future__ import annotations

APPLY_MODE_REVIEW = "review"
APPLY_MODE_AUTO = "auto"
VALID_APPLY_MODES = frozenset({APPLY_MODE_REVIEW, APPLY_MODE_AUTO})


def normalize_apply_mode(raw: str | None) -> str:
    mode = (raw or APPLY_MODE_REVIEW).strip().casefold()
    if mode in {"auto", "automatico", "automático", "full", "unattended"}:
        return APPLY_MODE_AUTO
    return APPLY_MODE_REVIEW


def apply_mode_is_auto(cfg: dict | None) -> bool:
    return normalize_apply_mode((cfg or {}).get("apply_mode")) == APPLY_MODE_AUTO


__all__ = [
    "APPLY_MODE_AUTO",
    "APPLY_MODE_REVIEW",
    "VALID_APPLY_MODES",
    "apply_mode_is_auto",
    "normalize_apply_mode",
]
