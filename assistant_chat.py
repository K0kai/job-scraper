"""Assistente do painel: pergunta de formulário → resposta pronta para colar.

Contexto = perfil do painel + fatos + dossiê dos currículos. Não grava em Fatos.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ai_client import call_ai_text
from candidate_context import panel_profile_block
from experience_priority import EXPERIENCE_PRIORITY_RULE
from resume_pipeline import AiUnavailableError, compose_analysis_dossier, get_resume

MAX_QUESTION_CHARS = 4000
MAX_HISTORY_TURNS = 8
MAX_ANSWER_CHARS = 2000
RESUME_BUDGET = 6000


def _diversity_block(cfg: dict[str, str]) -> str:
    mapping = (
        ("candidate_gender", "gender"),
        ("candidate_race", "race_ethnicity"),
        ("candidate_pcd", "pcd_disability"),
        ("candidate_lgbtq", "lgbtq"),
        ("candidate_diversity_note", "diversity_note"),
    )
    lines = []
    for key, label in mapping:
        value = str(cfg.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _resume_dossier(db: sqlite3.Connection, language: str) -> str:
    row = get_resume(db, language)
    if not row:
        return ""
    summary = (row["analysis_summary"] or "").strip()
    raw = (row["analysis_json"] or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data:
                dossier = compose_analysis_dossier(data, language)
                if dossier:
                    return dossier[:RESUME_BUDGET]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return summary[:RESUME_BUDGET]


def build_assistant_context(cfg: dict[str, str], db: sqlite3.Connection) -> str:
    """Monta o bloco de contexto (perfil + fatos + currículos) para o prompt."""
    parts: list[str] = []
    profile = panel_profile_block(cfg)
    if profile:
        parts.append("Panel profile:\n" + profile)
    diversity = _diversity_block(cfg)
    if diversity:
        parts.append("Diversity preferences:\n" + diversity)
    facts_pt = (cfg.get("candidate_facts_pt") or "").strip()
    facts_en = (cfg.get("candidate_facts_en") or "").strip()
    if facts_pt:
        parts.append("Candidate facts (PT):\n" + facts_pt[:3000])
    if facts_en:
        parts.append("Candidate facts (EN):\n" + facts_en[:3000])
    for lang, title in (("pt", "Resume dossier (PT)"), ("en", "Resume dossier (EN)")):
        dossier = _resume_dossier(db, lang)
        if dossier:
            parts.append(f"{title}:\n{dossier}")
    return "\n\n".join(parts).strip() or "[no profile or resume data available]"


def _normalize_history(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[-MAX_HISTORY_TURNS * 2 :]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().casefold()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        out.append({"role": role, "content": content[:MAX_QUESTION_CHARS]})
    return out


def build_assistant_prompt(
    *,
    question: str,
    context: str,
    history: list[dict[str, str]] | None = None,
) -> str:
    history_block = ""
    turns = history or []
    if turns:
        lines = []
        for turn in turns:
            label = "User" if turn["role"] == "user" else "Assistant"
            lines.append(f"{label}: {turn['content']}")
        history_block = "Prior turns in this chat:\n" + "\n".join(lines) + "\n\n"

    return f"""You help a job applicant fill application form fields.
Reply with ONLY the value they should paste into the form (or a short ready-to-paste answer).
Match the language of the question (Portuguese or English).
Use ONLY the candidate context below. Never invent employers, projects, metrics, skills, addresses, or identity facts.
If the context is insufficient, reply with a single short sentence starting with "FALTA:" (or "MISSING:" if the question is in English) naming exactly what is missing — do not invent a guess.
Do not wrap the answer in quotes unless the form value itself needs quotes.
Do not add explanations, markdown, or preamble.

{EXPERIENCE_PRIORITY_RULE}

Candidate context:
{context}

{history_block}Form question / label:
{question}
"""


def answer_form_question(
    *,
    question: str,
    cfg: dict[str, str],
    db: sqlite3.Connection,
    provider: str,
    model: str,
    api_key: str,
    history: Any = None,
) -> str:
    """Gera resposta pronta para colar. Levanta AiUnavailableError / ValueError."""
    q = (question or "").strip()
    if not q:
        raise ValueError("Cole a pergunta do formulário.")
    if len(q) > MAX_QUESTION_CHARS:
        raise ValueError(f"Pergunta muito longa (máx. {MAX_QUESTION_CHARS} caracteres).")
    if not api_key:
        raise AiUnavailableError("IA indisponível: chave não configurada.")

    context = build_assistant_context(cfg, db)
    prompt = build_assistant_prompt(
        question=q[:MAX_QUESTION_CHARS],
        context=context,
        history=_normalize_history(history),
    )
    answer = call_ai_text(
        prompt=prompt,
        provider=provider,
        model=model,
        api_key=api_key,
        max_output_tokens=800,
        temperature=0.2,
        timeout=90,
    )
    text = (answer or "").strip()
    if not text:
        raise ValueError("A IA não retornou resposta.")
    return text[:MAX_ANSWER_CHARS]
