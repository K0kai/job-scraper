"""Período salarial + proposta (FX) + normalização — puro/testável."""
from __future__ import annotations

import re
from typing import Literal

from form_rules import (
    _parse_amount,
    detect_contract_regime,
    detect_salary_currency,
    format_money,
    salary_for,
)

Period = Literal["year", "month", "hour", "unknown"]

#: pretensões do painel (BRL/USD) são tratadas como mensais
PANEL_PERIOD: Period = "month"

#: horas/ano padrão (40h × 52 sem) para conversão hora ↔ ano
HOURS_PER_YEAR = 2080

_YEAR_RE = re.compile(
    r"\b(annual|annually|yearly|per\s*year|/year|/yr|ano|anual|anuais|por\s*ano)\b",
    re.I,
)
_MONTH_RE = re.compile(
    r"\b(monthly|per\s*month|/month|/mo|mês|mes|mensal|mensais|por\s*m[eê]s)\b",
    re.I,
)
_HOUR_RE = re.compile(
    r"\b(hourly|per\s*hour|/hour|/hr|hora|horas|por\s*hora)\b",
    re.I,
)


def detect_salary_period(*hints: str) -> Period:
    hay = " ".join(h for h in hints if h)
    if not hay.strip():
        return "unknown"
    if _HOUR_RE.search(hay):
        return "hour"
    if _YEAR_RE.search(hay):
        return "year"
    if _MONTH_RE.search(hay):
        return "month"
    return "unknown"


def normalize_amount(amount: float, from_period: Period, to_period: Period) -> float:
    """Converte valor entre períodos. `unknown` = assume período do painel/mesma base."""
    if amount <= 0:
        return 0.0
    src = from_period if from_period != "unknown" else PANEL_PERIOD
    dst = to_period if to_period != "unknown" else src
    if src == dst:
        return float(amount)

    def to_year(n: float, p: Period) -> float:
        if p == "year":
            return n
        if p == "month":
            return n * 12.0
        if p == "hour":
            return n * HOURS_PER_YEAR
        return n

    def from_year(n: float, p: Period) -> float:
        if p == "year":
            return n
        if p == "month":
            return n / 12.0
        if p == "hour":
            return n / HOURS_PER_YEAR
        return n

    return from_year(to_year(amount, src), dst)


def propose_salary(
    cfg: dict[str, str],
    *,
    field_hint: str = "",
    job_text: str = "",
) -> dict:
    """Proposta do bot: moeda + período + valor formatado."""
    currency = detect_salary_currency(
        field_hint, job_text, preferred=cfg.get("salary_currency_preference", "")
    )
    regime = detect_contract_regime(field_hint, job_text)
    period = detect_salary_period(field_hint, job_text)
    raw = salary_for(cfg, currency, regime=regime)
    if not raw and currency == "USD":
        raw = salary_for(cfg, "BRL", regime=regime)
        if raw:
            currency = "BRL"
    amount = _parse_amount(raw) if raw else None
    if amount and amount > 0 and period not in {"unknown", PANEL_PERIOD}:
        converted = normalize_amount(amount, PANEL_PERIOD, period)
        formatted = format_money(converted, currency)
        return {
            "currency": currency,
            "period": period,
            "amount": converted,
            "formatted": formatted,
            "regime": regime,
            "panel_period": PANEL_PERIOD,
        }
    return {
        "currency": currency,
        "period": period,
        "amount": amount,
        "formatted": raw or "",
        "regime": regime,
        "panel_period": PANEL_PERIOD,
    }


__all__ = [
    "HOURS_PER_YEAR",
    "PANEL_PERIOD",
    "detect_salary_period",
    "normalize_amount",
    "propose_salary",
]
