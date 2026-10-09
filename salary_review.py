"""Review de IA sobre proposta salarial (política offshore / período)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from resume_pipeline import AiUnavailableError

LOG = logging.getLogger("odradek-scraper")

_SALARY_POLICY_RULES = """
Salary review policy (offshore / remote-from-Brazil candidate):
- Panel salary_brl / salary_usd are MONTHLY expectations for the candidate's
  normal mid-level target — NOT a hard floor for every role.
- Match the field's pay period (year / month / hour) and currency.
- Role tier (from job title/description):
  * intern / estágio / trainee: MUST lower an unrealistic panel proposal.
    Going BELOW the panel monthly expectation is required when the figure would
    look absurd for an internship (e.g. R$2000+ monthly for BR estágio). Prefer
    a realistic stipend/band for that market; do not invent luxury figures.
  * junior / entry-level: MAY lower below the panel floor when the proposal is
    clearly high for the role; stay plausible for junior pay in that country.
  * mid+ / standard: Floor = Brazilian monthly expectation converted to the
    field currency+period. Prefer panel USD remote anchor when present. Target
    a bit below local market (~10–20%) for offshore hires, but stay ABOVE the
    Brazil floor. Do not dump to local minimum wage.
- If the field is a select with options, pick the closest option text.
- If period/currency/market is too unclear to choose safely, action=ask.
""".strip()


def build_salary_review_prompt(
    *,
    field_hint: str,
    job_text: str,
    proposal: dict,
    options: list[str] | None,
    panel_profile: str,
    facts: str,
) -> str:
    opts = options or []
    return f"""You review a salary field fill for a job application.
Return ONLY one JSON object:
{{"action":"ok"|"adjust"|"ask","value":"<string if adjust>","period":"year|month|hour|unknown","question":"<if ask>","reason":"<short>"}}

{_SALARY_POLICY_RULES}

Field hint: {field_hint[:400]}
Job context (excerpt): {(job_text or "")[:1200]}
Bot proposal: {json.dumps(proposal, ensure_ascii=False)[:500]}
Select options (if any): {json.dumps(opts[:30], ensure_ascii=False)}
Panel profile: {(panel_profile or "")[:600]}
Candidate facts: {(facts or "")[:600]}
""".strip()


def parse_salary_review(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {"action": "ok", "reason": "empty"}
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {"action": "ok", "reason": "no-json"}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"action": "ok", "reason": "bad-json"}
    if not isinstance(data, dict):
        return {"action": "ok", "reason": "not-object"}
    action = str(data.get("action") or "ok").strip().casefold()
    if action not in {"ok", "adjust", "ask"}:
        action = "ok"
    return {
        "action": action,
        "value": str(data.get("value") or "").strip(),
        "period": str(data.get("period") or "").strip().casefold() or "unknown",
        "question": str(data.get("question") or "").strip(),
        "reason": str(data.get("reason") or "").strip()[:200],
    }


def review_salary_value(
    *,
    proposed_formatted: str,
    proposal: dict,
    field_hint: str,
    job_text: str,
    options: list[str] | None = None,
    cfg: dict[str, str] | None = None,
    ai: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Revisa a proposta. Retorna (valor_final, meta).

    Em falha de IA / 429: devolve a proposta do bot (não aborta).
    action=ask: se houver connect_fn, pergunta no painel; senão mantém proposta.
    """
    meta: dict[str, Any] = {"action": "ok", "reason": "no-ai"}
    value = (proposed_formatted or "").strip()
    if not value:
        return value, meta
    ai = ai or {}
    provider = str(ai.get("provider") or "")
    model = str(ai.get("model") or "")
    api_key = str(ai.get("api_key") or "")
    if not (provider and model and api_key):
        return value, meta

    from candidate_context import panel_profile_block
    from ai_client import call_ai_text

    prompt = build_salary_review_prompt(
        field_hint=field_hint,
        job_text=job_text,
        proposal=proposal,
        options=options,
        panel_profile=panel_profile_block(cfg or {}),
        facts=str(ai.get("facts") or ""),
    )
    try:
        raw = call_ai_text(
            prompt=prompt,
            provider=provider,
            model=model,
            api_key=api_key,
            max_output_tokens=250,
            temperature=0.1,
        )
    except AiUnavailableError as exc:
        LOG.warning("salary_review indisponivel (%s) — mantendo proposta do bot", exc)
        return value, {"action": "ok", "reason": f"ai-unavailable:{exc}"}
    except Exception as exc:
        LOG.warning("salary_review erro (%s) — mantendo proposta do bot", exc)
        return value, {"action": "ok", "reason": f"error:{exc}"}

    parsed = parse_salary_review(raw)
    meta = parsed
    action = parsed["action"]
    if action == "ok":
        return value, meta
    if action == "adjust" and parsed.get("value"):
        progress = ai.get("progress")
        if callable(progress):
            try:
                progress(f"salário ajustado pela IA: {parsed['value']}")
            except Exception:
                pass
        return str(parsed["value"]), meta
    if action == "ask":
        connect_fn = ai.get("connect_fn")
        question = parsed.get("question") or (
            f"Qual pretensão informar neste campo? (proposta: {value}; {field_hint[:120]})"
        )
        progress = ai.get("progress")
        if callable(progress):
            try:
                progress(f"perguntando salário: {question[:140]}")
            except Exception:
                pass
        if callable(connect_fn):
            try:
                from copilot_asks import (
                    STATUS_AWAITING_AI,
                    STATUS_ANSWERED,
                    complete_ask,
                    create_ask,
                    wait_for_answer,
                )

                job = ai.get("job") or {}
                job_id = None
                try:
                    if job.get("id") is not None:
                        job_id = int(job["id"])
                except (TypeError, ValueError):
                    job_id = None
                ask_id = create_ask(
                    connect_fn,
                    job_id=job_id,
                    question=question[:500],
                    now_iso=str(ai.get("now_iso") or "now"),
                )
                status, answer = wait_for_answer(connect_fn, ask_id, minutes=12)
                if status in {STATUS_AWAITING_AI, STATUS_ANSWERED} and (answer or "").strip():
                    complete_ask(connect_fn, ask_id)
                    if callable(progress):
                        try:
                            progress(f"salário respondido: {answer.strip()[:80]}")
                        except Exception:
                            pass
                    return answer.strip(), {**meta, "action": "ask", "answered": True}
            except Exception as exc:
                LOG.warning("salary_review ask falhou (%s) — mantendo proposta", exc)
        return value, {**meta, "answered": False}
    return value, meta


def needs_salary_ai_review(
    proposal: dict,
    *,
    options: list[str] | None = None,
    job_text: str = "",
    field_hint: str = "",
) -> bool:
    """Chama IA quando período ambíguo, select, moeda de mercado, ou intern/junior."""
    from salary_policy import detect_role_tier

    period = str(proposal.get("period") or "unknown").casefold()
    currency = str(proposal.get("currency") or "").upper()
    tier = str(proposal.get("tier") or "").casefold()
    if not tier or tier == "standard":
        tier = detect_role_tier(field_hint, job_text)
    if options:
        return True
    if period == "unknown":
        return True
    if tier in {"intern", "junior"}:
        return True
    # BRL/USD mid+ com período claro: FX+normalização bastam
    if currency in {"BRL", "USD"}:
        return False
    return True


def finalize_salary_value(
    cfg: dict[str, str],
    *,
    field_hint: str = "",
    job_text: str = "",
    options: list[str] | None = None,
    ai: dict[str, Any] | None = None,
    fallback: str = "",
) -> str:
    """Proposta do bot + review quando necessário (sempre para intern/junior)."""
    from salary_policy import propose_salary

    prop = propose_salary(cfg, field_hint=field_hint, job_text=job_text)
    proposed = str(prop.get("formatted") or fallback or "")
    if not needs_salary_ai_review(
        prop, options=options, job_text=job_text, field_hint=field_hint
    ):
        return proposed.strip()
    final, _meta = review_salary_value(
        proposed_formatted=proposed,
        proposal=prop,
        field_hint=field_hint,
        job_text=job_text,
        options=options,
        cfg=cfg,
        ai=ai,
    )
    return (final or proposed).strip()


__all__ = [
    "build_salary_review_prompt",
    "finalize_salary_value",
    "needs_salary_ai_review",
    "parse_salary_review",
    "review_salary_value",
]
