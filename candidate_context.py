"""Shared candidate profile context for assistant prompts."""
from __future__ import annotations


_PANEL_PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("candidate_name", "name"),
    ("candidate_email", "email"),
    ("candidate_phone", "phone"),
    ("candidate_linkedin", "linkedin"),
    ("candidate_city", "city"),
    ("candidate_street", "street"),
    ("candidate_postal_code", "postal_code"),
    ("candidate_state", "state"),
    ("candidate_country", "country"),
    ("candidate_current_company", "current_company"),
    ("candidate_cpf", "cpf"),
    ("salary_expectation_brl", "salary_brl"),
    ("salary_expectation_usd", "salary_usd"),
    ("candidate_contract_type", "contract_type"),
)


def panel_profile_block(cfg: dict | None) -> str:
    """Render nonempty profile values for the standalone assistant prompts."""
    cfg = cfg or {}
    lines = []
    for key, label in _PANEL_PROFILE_FIELDS:
        value = str(cfg.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    if any(str(cfg.get(key) or "").strip() for key in ("salary_expectation_brl", "salary_expectation_usd")):
        lines.append(
            "salary_note: panel amounts are MONTHLY mid-level targets; convert FX; "
            "match field period; for intern/junior LOWER unrealistic figures "
            "(may go below panel); for mid+ stay above Brazil floor and slightly "
            "under local market; prefer salary_usd when present"
        )
    return "\n".join(lines)
