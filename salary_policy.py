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
RoleTier = Literal["intern", "junior", "standard"]

#: pretensões do painel (BRL/USD) são tratadas como mensais
PANEL_PERIOD: Period = "month"

#: horas/ano padrão (40h × 52 sem) para conversão hora ↔ ano
HOURS_PER_YEAR = 2080

#: caps heurísticos mensais quando a vaga é estágio (antes da IA)
_INTERN_MONTHLY_CAP = {"BRL": 1200.0, "USD": 800.0}
_JUNIOR_MONTHLY_CAP = {"BRL": 4500.0, "USD": 2500.0}

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
_INTERN_RE = re.compile(
    r"\b(intern(ship)?|est[aá]gio|estagi[aá]ri[oa]|trainee|bolsista|"
    r"aprendiz|student\s+program|co-?op)\b",
    re.I,
)
_JUNIOR_RE = re.compile(
    r"\b(j[uú]nior|junior|jr\.?|entry[- ]?level|nivel\s*i\b|n[ií]vel\s*1|"
    r"associate\s+(engineer|developer)|graduate\s+program)\b",
    re.I,
)


def detect_role_tier(*texts: str) -> RoleTier:
    """intern | junior | standard a partir do título/descrição da vaga."""
    hay = " ".join(t for t in texts if t)
    if not hay.strip():
        return "standard"
    if _INTERN_RE.search(hay):
        return "intern"
    if _JUNIOR_RE.search(hay):
        return "junior"
    return "standard"


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


def apply_role_tier_cap(
    amount: float,
    *,
    currency: str,
    period: Period,
    tier: RoleTier,
) -> float:
    """Corta pretensão irrealista para intern/junior (valor na moeda+período do campo)."""
    if amount <= 0 or tier == "standard":
        return float(amount)
    code = (currency or "BRL").upper()
    caps = _INTERN_MONTHLY_CAP if tier == "intern" else _JUNIOR_MONTHLY_CAP
    monthly_cap = caps.get(code)
    if monthly_cap is None:
        # outras moedas: usa cap USD como âncora aproximada via amount já na moeda-alvo
        # (sem FX aqui — só evita números absurdos em BRL/USD; COP/EUR passam pela IA)
        return float(amount)
    dst = period if period != "unknown" else PANEL_PERIOD
    cap_in_field = normalize_amount(monthly_cap, PANEL_PERIOD, dst)
    return float(min(amount, cap_in_field))


def propose_salary(
    cfg: dict[str, str],
    *,
    field_hint: str = "",
    job_text: str = "",
) -> dict:
    """Proposta do bot: moeda + período + valor formatado (+ cap por tier)."""
    currency = detect_salary_currency(
        field_hint, job_text, preferred=cfg.get("salary_currency_preference", "")
    )
    regime = detect_contract_regime(field_hint, job_text)
    period = detect_salary_period(field_hint, job_text)
    tier = detect_role_tier(field_hint, job_text)
    raw = salary_for(cfg, currency, regime=regime)
    if not raw and currency == "USD":
        raw = salary_for(cfg, "BRL", regime=regime)
        if raw:
            currency = "BRL"
    amount = _parse_amount(raw) if raw else None
    if amount and amount > 0 and period not in {"unknown", PANEL_PERIOD}:
        converted = normalize_amount(amount, PANEL_PERIOD, period)
        converted = apply_role_tier_cap(
            converted, currency=currency, period=period, tier=tier
        )
        formatted = format_money(converted, currency)
        return {
            "currency": currency,
            "period": period,
            "amount": converted,
            "formatted": formatted,
            "regime": regime,
            "tier": tier,
            "panel_period": PANEL_PERIOD,
        }
    if amount and amount > 0:
        capped = apply_role_tier_cap(
            amount, currency=currency, period=period, tier=tier
        )
        if capped != amount:
            return {
                "currency": currency,
                "period": period,
                "amount": capped,
                "formatted": format_money(capped, currency),
                "regime": regime,
                "tier": tier,
                "panel_period": PANEL_PERIOD,
            }
    return {
        "currency": currency,
        "period": period,
        "amount": amount,
        "formatted": raw or "",
        "regime": regime,
        "tier": tier,
        "panel_period": PANEL_PERIOD,
    }


__all__ = [
    "HOURS_PER_YEAR",
    "PANEL_PERIOD",
    "apply_role_tier_cap",
    "detect_role_tier",
    "detect_salary_period",
    "normalize_amount",
    "propose_salary",
]
