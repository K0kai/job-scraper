# Radar de Vagas: interface local, armazenamento, busca e preparação de candidaturas.
# O servidor escuta apenas em 127.0.0.1 para manter o painel acessível somente neste computador.
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from apply_channels import apply_via_browser, apply_via_email, record_blocked, send_smtp_email
from form_rules import ensure_default_rules, list_rules, save_rules_from_form
from job_queue import KIND_APPLY, KIND_LINKEDIN, KIND_RESUME, JobQueue, NonRetryableError
from linkedin_apply import apply_via_linkedin, default_profile_dir
from ats_router import find_handler
from resume_pipeline import (
    AiUnavailableError,
    get_resume,
    mark_resume_analysis_error,
    persist_resume_upload,
    resume_match_snippets,
    resume_summaries,
    run_resume_analysis,
)
from panel_log import attach_to_logger, clear_logs, ensure_log_table, list_logs, log_event


ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "jobs.db")
RESUMES_DIR = os.path.join(ROOT, "resumes")
HOST = "127.0.0.1"
PORT = 8765
POLL_SECONDS = 15 * 60
LOG = logging.getLogger("job-scraper")

# Estrutura persistente. As vagas são deduplicadas pela impressão digital; cartas e execuções
# ficam separadas para poder registrar versões e resultados sem alterar os dados da vaga.
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT,
  title TEXT NOT NULL,
  company TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL,
  posted_at TEXT,
  first_seen_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  applied_at TEXT,
  notes TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_seen_idx ON jobs(first_seen_at DESC);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  state TEXT NOT NULL,
  found_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ai_decisions (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  decided_at TEXT NOT NULL,
  match_score INTEGER NOT NULL,
  should_apply INTEGER NOT NULL,
  letter_required INTEGER NOT NULL,
  reason TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "keywords": "software engineer, desenvolvedor, developer, backend, frontend, full stack, data engineer, devops",
    "locations": "remote, remoto, brazil, brasil, worldwide, anywhere",
    "sources": "remotive,remoteok",
    "interval_minutes": "15",
    "ai_provider": "gemini",
    "ai_model": "gemini-2.5-flash",
    "candidate_name": "",
    "candidate_email": "",
    "candidate_phone": "",
    "candidate_linkedin": "",
    "candidate_city": "",
    "candidate_cpf": "",
    # Expectativa salarial — o robô escolhe BRL×USD pela moeda do campo/vaga
    "salary_expectation_brl": "",
    "salary_expectation_usd": "",
    "salary_currency_preference": "auto",
    "salary_pj_multiplier": "1.3",
    # Preferência de contratação (radio Contractor/Employee); 'ask' = deixar com você
    "candidate_contract_type": "employee",
    "candidate_profile_pt": "",
    "candidate_profile_en": "",
    "candidate_facts_pt": "",
    "candidate_facts_en": "",
    "resume_pt_path": "",
    "resume_en_path": "",
    "auto_apply": "0",
    "browser_engine": "pydoll",
    "minimum_match_score": "65",
    "maximum_applications_per_run": "5",
    "adzuna_countries": "br,us,gb,ca",
    "apify_monthly_credit_limit_usd": "5",
    "apify_job_count": "25",
    "apify_actors_json": "[{\"id\": \"curious_coder~linkedin-jobs-scraper\", \"label\": \"LinkedIn Jobs\", \"enabled\": true, \"input_mode\": \"linkedin_search\", \"count\": 25}]",
    "apify_linkedin_filter_json": "",
    "apify_linkedin_filter_hash": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_from": "",
    "smtp_use_tls": "1",
    "queue_max_workers": "3",
    "queue_max_attempts": "40",
    "queue_ttl_hours": "24",
    # LinkedIn Easy Apply — assisted only (never auto-submit). ToS risk remains.
    "linkedin_easy_apply": "0",
    "linkedin_risk_ack": "0",
    "linkedin_chrome_profile": "",
    "linkedin_min_gap_minutes": "3",
    "linkedin_human_wait_minutes": "12",
    "linkedin_login_wait_minutes": "25",
    # Perfil de diversidade (identidade NUNCA vem de IA; 'not_informed' = não informar)
    "candidate_gender": "not_informed",
    "candidate_race": "not_informed",
    "candidate_lgbtq": "not_informed",
    "candidate_pcd": "no",
    "candidate_diversity_note": "",
}
STATUSES = {"new": "Nova", "review": "Na fila", "worth": "Vale a pena olhar", "saved": "Salva", "prepared": "Carta preparada", "applied": "Aplicada", "ignored": "Ignorada", "blocked": "Envio indisponível"}
BRASILIA = ZoneInfo("America/Sao_Paulo")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_brasilia(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "T" not in raw and " " not in raw and not raw.replace(".", "", 1).isdigit():
        # Date-only string without time — keep as calendar day when already dd-like, else parse.
        parsed = parse_utc(raw + "T00:00:00+00:00") if len(raw) >= 10 and raw[4] == "-" else None
        if parsed is None:
            return raw[:10]
        return parsed.astimezone(BRASILIA).strftime("%d/%m/%Y")
    parsed = parse_utc(raw)
    if parsed is None:
        return raw
    return parsed.astimezone(BRASILIA).strftime("%d/%m/%Y %H:%M")


def format_brasilia_date(value: object) -> str:
    """Data em Brasília no formato dd/mm/yyyy (sem hora)."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "T" not in raw and " " not in raw and not raw.replace(".", "", 1).isdigit():
        parsed = parse_utc(raw[:10] + "T12:00:00+00:00") if len(raw) >= 10 and raw[4] == "-" else None
        if parsed is None:
            return raw[:10]
        return parsed.astimezone(BRASILIA).strftime("%d/%m/%Y")
    parsed = parse_utc(raw)
    if parsed is None:
        return raw[:10]
    return parsed.astimezone(BRASILIA).strftime("%d/%m/%Y")


def connect() -> sqlite3.Connection:
    from dbutil import open_connection

    return open_connection(DB_PATH, timeout=60.0)


def initialize() -> None:
    """Cria o banco e aplica pequenas migrações compatíveis com versões anteriores."""
    os.makedirs(RESUMES_DIR, exist_ok=True)
    with connect() as db:
        db.executescript(SCHEMA)
        # Additions are applied in place so an existing jobs.db remains usable.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
        if "language" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN language TEXT NOT NULL DEFAULT 'unknown'")
        if "language_confidence" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN language_confidence REAL NOT NULL DEFAULT 0")
        db.execute("""CREATE TABLE IF NOT EXISTS cover_letters (
          id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
          language TEXT NOT NULL,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS resumes (
          id INTEGER PRIMARY KEY,
          language TEXT NOT NULL UNIQUE,
          original_filename TEXT NOT NULL DEFAULT '',
          stored_path TEXT NOT NULL,
          file_sha256 TEXT NOT NULL,
          extracted_text TEXT NOT NULL DEFAULT '',
          analysis_json TEXT NOT NULL DEFAULT '',
          analysis_summary TEXT NOT NULL DEFAULT '',
          analyzed_at TEXT,
          provider TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          analysis_status TEXT NOT NULL DEFAULT 'none',
          analysis_error TEXT NOT NULL DEFAULT '',
          analysis_message TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""")
        resume_cols = {row["name"] for row in db.execute("PRAGMA table_info(resumes)")}
        if "analysis_status" not in resume_cols:
            db.execute("ALTER TABLE resumes ADD COLUMN analysis_status TEXT NOT NULL DEFAULT 'none'")
        if "analysis_error" not in resume_cols:
            db.execute("ALTER TABLE resumes ADD COLUMN analysis_error TEXT NOT NULL DEFAULT ''")
        if "analysis_message" not in resume_cols:
            db.execute("ALTER TABLE resumes ADD COLUMN analysis_message TEXT NOT NULL DEFAULT ''")
        # Backfill: resumes with summary count as successful analysis.
        db.execute(
            """UPDATE resumes SET analysis_status='ok',
                   analysis_message=COALESCE(NULLIF(analysis_message,''), 'Análise concluída com sucesso.')
               WHERE IFNULL(analysis_summary,'') != '' AND analysis_status IN ('none', '')"""
        )
        db.execute("""CREATE TABLE IF NOT EXISTS form_field_rules (
          id INTEGER PRIMARY KEY,
          key TEXT NOT NULL,
          aliases TEXT NOT NULL DEFAULT '',
          mode TEXT NOT NULL DEFAULT 'text',
          value_from TEXT NOT NULL DEFAULT '',
          value TEXT NOT NULL DEFAULT '',
          sort_order INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          channel TEXT NOT NULL,
          recipient TEXT NOT NULL DEFAULT '',
          resume_id INTEGER,
          cover_letter_id INTEGER,
          status TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          attempted_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS form_answers (
          id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          question TEXT NOT NULL,
          answer TEXT NOT NULL,
          provider TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        )""")
        # Cache GLOBAL de respostas de IA por pergunta normalizada ('open' ou 'choices').
        db.execute("""CREATE TABLE IF NOT EXISTS ai_answer_cache (
          id INTEGER PRIMARY KEY,
          question_norm TEXT NOT NULL UNIQUE,
          question_raw TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'open',
          answer TEXT NOT NULL,
          provider TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          hits INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""")
        decision_cols = {row["name"] for row in db.execute("PRAGMA table_info(ai_decisions)")}
        if "resume_language" not in decision_cols:
            db.execute("ALTER TABLE ai_decisions ADD COLUMN resume_language TEXT NOT NULL DEFAULT ''")
        if "apply_channel" not in decision_cols:
            db.execute("ALTER TABLE ai_decisions ADD COLUMN apply_channel TEXT NOT NULL DEFAULT ''")
        if "apply_result" not in decision_cols:
            db.execute("ALTER TABLE ai_decisions ADD COLUMN apply_result TEXT NOT NULL DEFAULT ''")
        ensure_default_rules(db)
        for key, value in DEFAULT_SETTINGS.items():
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))
        # Default antigo (80) era rígido demais com a triagem generosa.
        db.execute(
            "UPDATE settings SET value='65' WHERE key='minimum_match_score' AND value='80'"
        )
        # Uma vez: regenerar filtro LinkedIn com geo remote global + híbrido BH.
        row = db.execute(
            "SELECT value FROM settings WHERE key='apify_linkedin_geo_v2'"
        ).fetchone()
        if not row or row["value"] != "1":
            db.execute("UPDATE settings SET value='' WHERE key='apify_linkedin_filter_hash'")
            db.execute(
                "INSERT INTO settings(key,value) VALUES('apify_linkedin_geo_v2','1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        ensure_log_table(db)


def settings() -> dict[str, str]:
    """Lê as preferências salvas no SQLite."""
    with connect() as db:
        return {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM settings")}


def save_settings(data: dict[str, str]) -> None:
    """Atualiza somente os campos enviados pelo formulário atual."""
    with connect() as db:
        for key in DEFAULT_SETTINGS:
            if key in data:
                db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, data.get(key, "")))


def set_setting(key: str, value: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def detect_language(text: str) -> tuple[str, float]:
    """Detect English/Portuguese locally, using Lingua when installed."""
    sample = re.sub(r"<[^>]+>", " ", text or "")
    sample = re.sub(r"\s+", " ", sample).strip()[:12000]
    if len(sample) < 30:
        return "unknown", 0.0
    try:
        from lingua import Language, LanguageDetectorBuilder
        detector = LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.PORTUGUESE).build()
        scores = detector.compute_language_confidence_values(sample)
        if not scores:
            return "unknown", 0.0
        best = scores[0]
        confidence = float(best.value)
        return ("en" if best.language == Language.ENGLISH else "pt", confidence) if confidence >= 0.65 else ("unknown", confidence)
    except ImportError:
        # A conservative fallback keeps the app usable before optional model install.
        normalized = " " + re.sub(r"[^a-záàâãéêíóôõúüç ]", " ", sample.casefold()) + " "
        pt = sum(normalized.count(f" {word} ") for word in ("de", "para", "com", "uma", "não", "você", "experiência", "responsabilidades", "requisitos", "benefícios", "trabalho"))
        en = sum(normalized.count(f" {word} ") for word in ("the", "and", "with", "for", "you", "experience", "responsibilities", "requirements", "benefits", "work", "team"))
        total = pt + en
        if total < 2 or pt == en:
            return "unknown", 0.0
        return ("pt", min(0.9, 0.55 + pt / (total + 2) * 0.4)) if pt > en else ("en", min(0.9, 0.55 + en / (total + 2) * 0.4))


def ai_generate_letter(provider: str, model: str, language: str, job: dict, cfg: dict[str, str]) -> str:
    """Pede somente a carta; o modelo não recebe, cria ou altera o arquivo de currículo."""
    provider = provider.casefold().strip()
    language_name = "Portuguese" if language == "pt" else "English"
    facts_key = "candidate_facts_pt" if language == "pt" else "candidate_facts_en"
    facts = cfg.get(facts_key, "").strip()
    name = cfg.get("candidate_name", "").strip()
    prompt = f"""Write a concise, specific cover letter in {language_name} for this job application.
Use only the candidate facts provided below. Never invent experience, qualifications, results, employers, dates, or skills. If facts are sparse, keep the letter brief and make no unsupported claims. Do not claim the candidate already applied. Return only the letter, with no subject line or commentary.

Candidate name: {name or '[candidate name]'}
Candidate-provided facts:
{facts or '[No candidate facts provided.]'}
Resume analysis summary (authoritative; do not invent beyond this):
{cfg.get("resume_summary_pt" if language == "pt" else "resume_summary_en", "") or "[No resume analysis provided.]"}

Job title: {job['title']}
Company: {job['company']}
Location / eligibility: {job['location']}
Job description:
{re.sub(r'<[^>]+>', ' ', job['description'])[:10000]}
"""
    if provider == "openai":
        api_key = get_ai_key(provider)
        endpoint = "https://api.openai.com/v1/responses"
        payload = json.dumps({"model": model, "input": prompt, "store": False, "max_output_tokens": 700}).encode("utf-8")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    elif provider == "gemini":
        api_key = get_ai_key(provider)
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
        payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 700, "temperature": 0.4}}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        raise ValueError("Provedor de IA deve ser Gemini ou OpenAI.")
    if not api_key:
        raise ValueError("Configure a chave da API na seção de IA.")
    request = Request(endpoint, data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code in {401, 403, 429, 502, 503, 504} or "quota" in body.casefold():
            raise AiUnavailableError(f"IA indisponível ao gerar carta (HTTP {exc.code}).") from exc
        raise RuntimeError(f"Falha ao chamar a API de IA (HTTP {exc.code}).") from exc
    except Exception as exc:
        msg = str(exc).casefold()
        if any(t in msg for t in ("429", "quota", "unavailable", "timeout")):
            raise AiUnavailableError(f"IA indisponível ao gerar carta: {exc}") from exc
        raise RuntimeError(f"Falha ao chamar a API de IA ({exc.__class__.__name__}).") from exc
    if provider == "openai":
        parts = [part.get("text", "") for item in result.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text"]
        letter = "\n".join(parts).strip()
    else:
        letter = "\n".join(part.get("text", "") for item in result.get("candidates", []) for part in item.get("content", {}).get("parts", [])).strip()
    if not letter:
        raise RuntimeError("A API não retornou uma carta de apresentação.")
    return letter


def get_ai_key(provider: str) -> str:
    # The key is kept in the operating system credential vault, never in SQLite.
    try:
        import keyring
        return keyring.get_password("job-scraper", provider) or ""
    except Exception:
        return ""


def save_ai_key(provider: str, key: str) -> None:
    try:
        import keyring
        if key.strip():
            keyring.set_password("job-scraper", provider, key.strip())
        else:
            keyring.delete_password("job-scraper", provider)
    except Exception as exc:
        raise RuntimeError("Não foi possível acessar o cofre seguro do sistema. Instale as dependências do app.") from exc


def secret_get(name: str) -> str:
    try:
        import keyring
        return keyring.get_password("job-scraper", name) or ""
    except Exception:
        return ""


def secret_set(name: str, value: str) -> None:
    try:
        import keyring
        if value.strip():
            keyring.set_password("job-scraper", name, value.strip())
    except Exception as exc:
        raise RuntimeError("Não foi possível acessar o cofre seguro do sistema.") from exc


def create_cover_letter(job_id: int, resume_language: str | None = None) -> int:
    cfg = settings()
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        summaries = resume_summaries(db)
    if not job:
        raise ValueError("Vaga não encontrada.")
    language = resume_language or job["language"]
    if language not in ("pt", "en"):
        raise ValueError("Idioma da vaga incerto. Corrija o idioma antes de gerar a carta.")
    cfg = dict(cfg)
    cfg["resume_summary_pt"] = summaries.get("pt", "")
    cfg["resume_summary_en"] = summaries.get("en", "")
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    letter = ai_generate_letter(provider, model, language, dict(job), cfg)
    stamp = now_iso()
    with connect() as db:
        db.execute("""INSERT INTO cover_letters(job_id,language,provider,model,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(job_id) DO UPDATE SET language=excluded.language,provider=excluded.provider,model=excluded.model,body=excluded.body,updated_at=excluded.updated_at""", (job_id, language, provider, model, letter, stamp, stamp))
        db.execute("UPDATE jobs SET status='prepared' WHERE id=? AND status IN ('new','review')", (job_id,))
        row = db.execute("SELECT id FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
    return int(row["id"])


def ai_assess_job(job: dict, cfg: dict[str, str]) -> dict:
    """Classifica aderência usando a análise de currículo já salva (sem reenviar o PDF)."""
    language = job.get("language", "unknown")
    if language not in ("pt", "en"):
        raise ValueError("Idioma incerto; a IA não vai escolher um currículo por suposição.")
    facts = cfg.get("candidate_facts_pt" if language == "pt" else "candidate_facts_en", "").strip()
    with connect() as db:
        summaries = resume_summaries(db)
        snippets = resume_match_snippets(db)
    resume_summary_pt = summaries.get("pt", "").strip()
    resume_summary_en = summaries.get("en", "").strip()
    snippet_pt = snippets.get("pt", "").strip()
    snippet_en = snippets.get("en", "").strip()
    if language == "pt" and not resume_summary_pt and not resume_summary_en and not snippet_pt and not snippet_en:
        raise ValueError("Faça upload e análise de pelo menos um currículo antes da triagem automática.")
    if language == "en" and not resume_summary_en and not resume_summary_pt and not snippet_en and not snippet_pt:
        raise ValueError("Faça upload e análise de pelo menos um currículo antes da triagem automática.")

    prompt = f"""You are an ENTHUSIASTIC job-application coach. Your bias is to APPLY when there is any credible path.

CRITICAL CALIBRATION (follow strictly):
- Be GENEROUS with match_score. Most plausible tech/office/remote roles for this candidate should land 65–90, not 20–40.
- If the job is in the SAME broad field as the resume (e.g. software/engineering/IT/data/web vs software resume), match_score MUST be >= 65 unless a hard blocker applies.
- If the job title overlaps the candidate's roles/skills even partially, match_score MUST be >= 70.
- Missing tools from a long JD list is NORMAL — do NOT tank the score. Penalize at most 5–15 points total for missing secondary tools.
- Prefer transferable and adjacent skills (React↔Vue, AWS↔GCP, SQL dialects, similar frameworks).
- Do NOT invent employers, degrees, or tools absent from the resume context.
- Default to should_apply=true whenever match_score >= 50 and no hard blocker.

Scoring bands (use the HIGH end when unsure):
- 85–100: core role fits; several overlapping skills.
- 70–84: good enough to apply; partial stack overlap or transferable skills.
- 55–69: stretch / adjacent role in the same field — still apply.
- 40–54: weak but same industry; apply only if remote/flexible.
- 0–39: ONLY for hard blockers or totally different careers (nurse, truck driver, accountant with no path, etc.).

Hard blockers (should_apply=false, score usually <40):
- Fundamentally different profession with no bridge from the resume.
- Explicit non-negotiable visa/onsite conflict vs candidate facts/location notes.
- Seniority jump of ~2+ levels with zero supporting evidence.

Never treat as hard blockers: laundry-list tools, "nice to have", cover letter requested, imperfect keyword match, years stated as "X+" when candidate is close.

Identify whether the job description asks for a cover letter.
Return ONLY JSON with keys:
match_score (integer 0-100),
should_apply (boolean),
cover_letter_required (boolean),
reason (short string in {('Portuguese' if language == 'pt' else 'English')}),
recommended_resume_language (either "en" or "pt").

Candidate facts: {facts or '[none provided]'}
Resume profile snippet (PT): {snippet_pt or '[none provided]'}
Resume profile snippet (EN): {snippet_en or '[none provided]'}
Resume summary (PT): {(resume_summary_pt[:2500] if resume_summary_pt else '[none provided]')}
Resume summary (EN): {(resume_summary_en[:2500] if resume_summary_en else '[none provided]')}
Job title: {job['title']}
Company: {job['company']}
Location/eligibility: {job['location']}
Description: {re.sub(r'<[^>]+>', ' ', job['description'])[:10000]}"""

    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    api_key = get_ai_key(provider)
    if not api_key:
        raise AiUnavailableError("Configure a chave da API de IA para ativar o julgamento automático.")
    try:
        if provider == "openai":
            endpoint = "https://api.openai.com/v1/responses"
            payload = json.dumps(
                {
                    "model": model,
                    "input": prompt,
                    "text": {"format": {"type": "json_object"}},
                    "store": False,
                    "max_output_tokens": 400,
                    "temperature": 0.7,
                }
            ).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == "gemini":
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
            payload = json.dumps(
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": 400,
                        "temperature": 0.7,
                        "responseMimeType": "application/json",
                    },
                }
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            raise ValueError("Provedor de IA inválido.")
        request = Request(endpoint, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code in {401, 403, 429} or "quota" in body.casefold():
            raise AiUnavailableError(f"IA indisponível (HTTP {exc.code}).") from exc
        raise
    if provider == "openai":
        raw = "\n".join(part.get("text", "") for item in result.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text")
    else:
        raw = "\n".join(part.get("text", "") for item in result.get("candidates", []) for part in item.get("content", {}).get("parts", []))
    decision = json.loads(raw.strip())

    recommended_language = decision.get("recommended_resume_language")
    if recommended_language not in ("en", "pt"):
        match_score = int(decision.get("match_score", 0))
        if language == "en" and match_score >= 65 and resume_summary_en:
            recommended_language = "en"
        elif resume_summary_pt:
            recommended_language = "pt"
        elif resume_summary_en:
            recommended_language = "en"
        else:
            recommended_language = language
    if recommended_language == "pt" and not resume_summary_pt and resume_summary_en:
        recommended_language = "en"
    if recommended_language == "en" and not resume_summary_en and resume_summary_pt:
        recommended_language = "pt"

    score = max(0, min(100, int(decision.get("match_score", 0))))
    should_apply = bool(decision.get("should_apply"))
    # Soft floor: same-field optimistic calibration if the model still underrates.
    if should_apply and score < 50:
        score = 50
    elif score >= 50:
        should_apply = True
    return {
        "match_score": score,
        "should_apply": should_apply,
        "letter_required": bool(decision.get("cover_letter_required")),
        "reason": str(decision.get("reason", ""))[:1000],
        "provider": provider,
        "model": model,
        "resume_language": recommended_language,
    }



def is_linkedin_job(job: dict) -> bool:
    blob = " ".join(
        str(job.get(key) or "")
        for key in ("url", "source", "description", "company")
    ).casefold()
    return "linkedin.com" in blob or "linkedin" in str(job.get("source") or "").casefold()


def is_assisted_apply_job(job: dict) -> bool:
    """LinkedIn Easy Apply / external Apply, ou carreira direta de ATS suportado."""
    if is_linkedin_job(job):
        return True
    return find_handler(str(job.get("url") or "")) is not None


def mark_worth_looking(job_id: int, *, score: int, reason: str, detail: str) -> None:
    note = (
        f"Vale a pena olhar (score {score}/100). {detail} "
        f"Motivo da IA: {reason}"
    )[:900]
    with connect() as db:
        db.execute(
            "UPDATE jobs SET status='worth', notes=? WHERE id=?",
            (note, job_id),
        )
        db.execute(
            "UPDATE ai_decisions SET apply_channel=?, apply_result=? WHERE id=(SELECT MAX(id) FROM ai_decisions WHERE job_id=?)",
            ("manual", detail[:500], job_id),
        )
    log_event("info", "worth", f"Vaga #{job_id} enviada para 'Vale a pena olhar' — {detail}")


def process_auto_job(job_id: int) -> str:

    """Triagem com análise salva do currículo; aplica por e-mail ou Playwright quando possível."""
    cfg = settings()
    with connect() as db:
        job_row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job_row:
        return "vaga removida"
    job = dict(job_row)
    title = job.get("title", "")
    log_event("info", "auto-apply", f"Triagem da vaga #{job_id}: {title}")
    try:
        decision = ai_assess_job(job, cfg)
    except AiUnavailableError:
        # Propagates to the job queue for retry/backoff.
        raise
    except ValueError as exc:
        with connect() as db:
            db.execute("UPDATE jobs SET status='blocked', notes=? WHERE id=?", (str(exc), job_id))
        log_event("warning", "auto-apply", f"Vaga #{job_id}: {exc}")
        return f"vaga {job_id}: {exc}"

    minimum = max(0, min(100, int(cfg.get("minimum_match_score", "80") or 80)))
    qualifies = decision["should_apply"] and decision["match_score"] >= minimum
    resume_language = decision["resume_language"]
    with connect() as db:
        db.execute(
            """INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason,resume_language,apply_channel,apply_result)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (job_id, now_iso(), decision["match_score"], int(qualifies), int(decision["letter_required"]), decision["reason"], resume_language, "", ""),
        )
    log_event(
        "info",
        "auto-apply",
        f"Vaga #{job_id}: score {decision['match_score']}/100, should_apply={decision['should_apply']}, CV={resume_language}. {decision['reason']}",
    )
    if not qualifies:
        with connect() as db:
            db.execute("UPDATE jobs SET status='ignored', notes=? WHERE id=?", (f"IA: score {decision['match_score']}/100. {decision['reason']}", job_id))
        log_event("info", "auto-apply", f"Vaga #{job_id} ignorada (score abaixo do mínimo ou should_apply=false).")
        return f"IA descartou vaga {job_id} ({decision['match_score']}/100)"

    with connect() as db:
        resume = get_resume(db, resume_language)
    if not resume or not (resume["analysis_summary"] or "").strip():
        with connect() as db:
            db.execute("UPDATE jobs SET status='blocked', notes=? WHERE id=?", (f"Sem currículo analisado em {resume_language}.", job_id))
        return f"vaga {job_id}: currículo {resume_language} ausente"

    try:
        cover_id = create_cover_letter(job_id, resume_language=resume_language)
    except AiUnavailableError:
        raise
    except Exception as exc:
        with connect() as db:
            db.execute("UPDATE jobs SET status='blocked', notes=? WHERE id=?", (f"Falha ao gerar carta: {exc}", job_id))
        return f"vaga {job_id}: falha na carta"

    with connect() as db:
        letter_row = db.execute("SELECT * FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
    cover_body = letter_row["body"] if letter_row else ""
    stamp = now_iso()
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    api_key = get_ai_key(provider)
    score = int(decision["match_score"])

    # LinkedIn: optional Easy Apply (supervised Playwright + Chrome profile), else worth-looking.
    if is_linkedin_job(job):
        if cfg.get("linkedin_easy_apply") == "1":
            ok, detail = apply_via_linkedin(
                connect,
                job,
                cfg,
                cover_letter=cover_body,
                resume_path=resume["stored_path"],
                resume_id=int(resume["id"]),
                cover_letter_id=cover_id,
                resume_summary=resume["analysis_summary"] or "",
                resume_json=resume["analysis_json"] or "",
                provider=provider,
                model=model,
                api_key=api_key,
                now_iso=stamp,
                project_root=ROOT,
            )
            channel = "linkedin"
            if ok:
                dry = "dry-run" in detail.casefold()
                human_sent = detail.casefold().startswith(("assisted:", "auto:"))
                # Sai de 'worth' só com sucesso real; dry-run/timeout permanece lá
                # para nova tentativa (nota atualizada com o que aconteceu).
                job_status = "applied" if human_sent else ("worth" if dry else "applied")
                with connect() as db:
                    if job_status == "applied":
                        db.execute(
                            "UPDATE jobs SET status=?, applied_at=?, notes=? WHERE id=?",
                            (job_status, now_iso(), detail[:900], job_id),
                        )
                    else:
                        db.execute(
                            "UPDATE jobs SET status=?, notes=? WHERE id=?",
                            (job_status, detail[:900], job_id),
                        )
                    db.execute(
                        "UPDATE ai_decisions SET apply_channel=?, apply_result=? WHERE id=(SELECT MAX(id) FROM ai_decisions WHERE job_id=?)",
                        (channel, detail[:500], job_id),
                    )
                log_event(
                    "success" if human_sent else "info",
                    "auto-apply",
                    f"Vaga #{job_id} LinkedIn Easy Apply: {detail}",
                )
                return f"vaga {job_id}: {detail}"
            record_blocked(connect, job_id, detail, now_iso(), int(resume["id"]), cover_id)
            mark_worth_looking(
                job_id,
                score=score,
                reason=decision["reason"],
                detail=f"Easy Apply LinkedIn falhou ou indisponível: {detail}",
            )
            return f"vaga {job_id}: vale a pena olhar — {detail}"
        mark_worth_looking(
            job_id,
            score=score,
            reason=decision["reason"],
            detail="LinkedIn detectado — Easy Apply desligado; carta preparada para envio manual.",
        )
        return f"vaga {job_id}: vale a pena olhar (LinkedIn, score {score})"

    ok, detail = apply_via_email(
        connect,
        job,
        cfg,
        cover_letter=cover_body,
        resume_path=resume["stored_path"],
        resume_id=int(resume["id"]),
        cover_letter_id=cover_id,
        smtp_password=secret_get("smtp_password"),
        now_iso=stamp,
    )
    channel = "email"
    if not ok:
        ok, detail = apply_via_browser(
            connect,
            job,
            cfg,
            cover_letter=cover_body,
            resume_path=resume["stored_path"],
            resume_id=int(resume["id"]),
            cover_letter_id=cover_id,
            resume_summary=resume["analysis_summary"] or "",
            resume_json=resume["analysis_json"] or "",
            provider=provider,
            model=model,
            api_key=api_key,
            now_iso=now_iso(),
        )
        channel = "browser"
    if not ok:
        record_blocked(connect, job_id, detail, now_iso(), int(resume["id"]), cover_id)
        mark_worth_looking(
            job_id,
            score=score,
            reason=decision["reason"],
            detail=f"Match bom, mas envio automático falhou ({channel}): {detail}",
        )
        return f"vaga {job_id}: vale a pena olhar — {detail}"
    with connect() as db:
        db.execute("UPDATE jobs SET status='applied', applied_at=?, notes=? WHERE id=?", (now_iso(), detail[:900], job_id))
        db.execute(
            "UPDATE ai_decisions SET apply_channel=?, apply_result=? WHERE id=(SELECT MAX(id) FROM ai_decisions WHERE job_id=?)",
            (channel, detail[:500], job_id),
        )
    log_event("success", "auto-apply", f"Vaga #{job_id} aplicada via {channel}: {detail}")
    return f"vaga {job_id}: aplicada via {channel} — {detail}"


def process_auto_apply_batch() -> str:
    """Enfileira todas as vagas novas; o paralelismo fica a cargo de queue_max_workers."""
    cfg = settings()
    if cfg.get("auto_apply") != "1":
        return "auto_apply desligado"
    with connect() as db:
        rows = db.execute("SELECT id FROM jobs WHERE status='new' ORDER BY id ASC").fetchall()
    if not rows:
        return "nenhuma vaga nova para enfileirar"
    queued = 0
    for row in rows:
        job_id = int(row["id"])
        queue.enqueue(KIND_APPLY, {"job_id": job_id}, dedupe_key=f"apply:{job_id}")
        with connect() as db:
            db.execute(
                "UPDATE jobs SET status='review', notes=? WHERE id=? AND status='new'",
                ("Na fila de triagem/candidatura (processada pelos workers da fila).", job_id),
            )
        queued += 1
    workers = cfg.get("queue_max_workers", "3")
    return f"{queued} vaga(s) enfileirada(s) para triagem (até {workers} worker(s) em paralelo)"


def handle_resume_analysis_job(payload: dict) -> str:
    language = str(payload.get("language") or "").casefold()
    cfg = settings()
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    stamp = now_iso()
    try:
        with connect() as db:
            return run_resume_analysis(
                db,
                language=language,
                provider=provider,
                model=model,
                api_key=get_ai_key(provider),
                now_iso=stamp,
            )
    except AiUnavailableError:
        raise
    except Exception as e:
        with connect() as db:
            mark_resume_analysis_error(db, language, f"{e.__class__.__name__}: {e}", stamp)
        raise


def handle_job_apply_job(payload: dict) -> str:
    job_id = int(payload.get("job_id") or 0)
    if not job_id:
        raise ValueError("job_id ausente no payload da fila.")
    return process_auto_job(job_id)


def process_worth_easy_apply(job_id: int) -> str:
    """Easy Apply assistido a partir de 'Vale a pena olhar' — sem nova busca/triagem."""
    cfg = settings()
    if cfg.get("linkedin_easy_apply") != "1":
        raise ValueError("Ative Easy Apply LinkedIn (e o aceite de risco) no painel.")
    if cfg.get("linkedin_risk_ack") != "1":
        raise ValueError("Confirme o aviso de risco/ToS no painel antes do Easy Apply.")

    with connect() as db:
        job_row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job_row:
        raise ValueError("Vaga removida.")
    job = dict(job_row)
    if job.get("status") not in {"worth", "prepared", "review", "blocked"}:
        raise ValueError(f"Status '{job.get('status')}' não permite Easy Apply manual.")
    if not is_linkedin_job(job) and find_handler(str(job.get("url") or "")) is None:
        raise ValueError("Esta vaga não é LinkedIn nem ATS suportado — use e-mail/formulário ou candidatura manual.")

    with connect() as db:
        decision = db.execute(
            """SELECT match_score, reason, resume_language FROM ai_decisions
               WHERE job_id=? ORDER BY id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        letter_row = db.execute("SELECT * FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()

    resume_language = (
        (decision["resume_language"] if decision else None)
        or job.get("language")
        or "en"
    ).casefold()
    if resume_language not in {"pt", "en"}:
        resume_language = "en"

    with connect() as db:
        resume = get_resume(db, resume_language)
    if not resume or not (resume["analysis_summary"] or "").strip():
        raise ValueError(f"Sem currículo analisado em {resume_language}.")

    cover_id = int(letter_row["id"]) if letter_row else None
    cover_body = letter_row["body"] if letter_row else ""
    if not cover_body.strip():
        try:
            cover_id = create_cover_letter(job_id, resume_language=resume_language)
        except AiUnavailableError:
            raise
        with connect() as db:
            letter_row = db.execute("SELECT * FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
        cover_body = letter_row["body"] if letter_row else ""
        cover_id = int(letter_row["id"]) if letter_row else cover_id

    stamp = now_iso()
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    api_key = get_ai_key(provider)
    score = int(decision["match_score"]) if decision and decision["match_score"] is not None else 0
    reason = (decision["reason"] if decision else "") or "Easy Apply manual a partir de Vale a pena olhar"

    log_event("info", "linkedin", f"Easy Apply manual enfileirado/iniciado para vaga #{job_id}: {job.get('title')}")
    ok, detail = apply_via_linkedin(
        connect,
        job,
        cfg,
        cover_letter=cover_body,
        resume_path=resume["stored_path"],
        resume_id=int(resume["id"]),
        cover_letter_id=cover_id,
        resume_summary=resume["analysis_summary"] or "",
        resume_json=resume["analysis_json"] or "",
        provider=provider,
        model=model,
        api_key=api_key,
        now_iso=stamp,
        project_root=ROOT,
    )
    channel = "linkedin"
    if ok:
        dry = "dry-run" in detail.casefold()
        human_sent = detail.casefold().startswith(("assisted:", "auto:"))
        # Mesma regra do fluxo automático: só sucesso real tira a vaga do worth.
        job_status = "applied" if human_sent else ("worth" if dry else "applied")
        with connect() as db:
            if job_status == "applied":
                db.execute(
                    "UPDATE jobs SET status=?, applied_at=?, notes=? WHERE id=?",
                    (job_status, now_iso(), detail[:900], job_id),
                )
            else:
                db.execute(
                    "UPDATE jobs SET status=?, notes=? WHERE id=?",
                    (job_status, detail[:900], job_id),
                )
            db.execute(
                """INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason,resume_language,apply_channel,apply_result)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (job_id, now_iso(), score, 1, 1, reason[:1000], resume_language, channel, detail[:500]),
            )
        log_event(
            "success" if human_sent else "info",
            "linkedin",
            f"Vaga #{job_id} Easy Apply (worth): {detail}",
        )
        return f"vaga {job_id}: {detail}"

    record_blocked(connect, job_id, detail, now_iso(), int(resume["id"]), cover_id)
    mark_worth_looking(
        job_id,
        score=score,
        reason=reason,
        detail=f"Easy Apply LinkedIn falhou ou indisponível: {detail}",
    )
    # Queue must show failed (not succeeded) — do not auto-retry auth/UI misses.
    raise NonRetryableError(f"Easy Apply sem sucesso (vaga #{job_id}): {detail}")


def handle_linkedin_apply_job(payload: dict) -> str:
    job_id = int(payload.get("job_id") or 0)
    if not job_id:
        raise ValueError("job_id ausente no payload da fila.")
    return process_worth_easy_apply(job_id)


def enqueue_worth_easy_apply(job_id: int) -> str:
    """Valida e enfileira Easy Apply; o worker abre o Chrome (não bloqueia o POST)."""
    cfg = settings()
    if cfg.get("linkedin_easy_apply") != "1":
        return "Ative Easy Apply LinkedIn no painel (Perfil/IA)."
    if cfg.get("linkedin_risk_ack") != "1":
        return "Confirme o aviso de risco/ToS no painel antes de usar Easy Apply."
    with connect() as db:
        job_row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job_row:
        return "Vaga não encontrada."
    job = dict(job_row)
    if job.get("status") not in {"worth", "prepared", "blocked"}:
        return f"Só é possível Easy Apply a partir de Vale a pena olhar (status atual: {job.get('status')})."
    if not is_assisted_apply_job(job):
        return "Esta vaga não parece LinkedIn nem ATS suportado (InHire, Greenhouse, Lever, Gupy)."
    qid = queue.enqueue(KIND_LINKEDIN, {"job_id": job_id}, dedupe_key=f"linkedin:{job_id}")
    with connect() as db:
        # A vaga PERMANECE em 'worth' enquanto está na fila/processando — sair de
        # lá só com resultado real (enviada). Se o worker morrer, ela não sumiu.
        db.execute(
            "UPDATE jobs SET notes=? WHERE id=? AND status IN ('worth','prepared','blocked')",
            (
                f"Na fila do Easy Apply (fila #{qid}) — a vaga continua em Vale a pena olhar "
                "ate sair o resultado. Fique atento à janela do Chrome.",
                job_id,
            ),
        )
    log_event("info", "linkedin", f"Vaga #{job_id} enfileirada para Easy Apply/InHire (fila #{qid}).")
    return f"Candidatura assistida enfileirada (fila #{qid}). No Chrome: revise, captcha e Enviar são com você."


queue = JobQueue(
    db_path=DB_PATH,
    handlers={
        KIND_RESUME: handle_resume_analysis_job,
        KIND_APPLY: handle_job_apply_job,
        KIND_LINKEDIN: handle_linkedin_apply_job,
    },
    get_settings=settings,
    connect_fn=connect,
)


def terms(value: str) -> list[str]:
    return [part.strip().casefold() for part in value.split(",") if part.strip()]


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/')}"


def fingerprint(job: dict) -> str:
    stable_url = normalize_url(job.get("url", ""))
    text = "|".join((stable_url, job.get("company", "").casefold().strip(), job.get("title", "").casefold().strip(), job.get("location", "").casefold().strip()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_json(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None, timeout: int = 25) -> object:
    """Busca uma resposta JSON com timeout e identificação do cliente."""
    request_headers = {"User-Agent": "JobScraperLocal/0.1 (personal job search)", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=data, headers=request_headers, method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Falha de rede ({exc.reason})") from exc
    if not raw.strip():
        return {}
    return json.loads(raw)


def fetch_remotive() -> list[dict]:
    """Adapta os anúncios do feed público Remotive ao formato comum do banco."""
    data = fetch_json("https://remotive.com/api/remote-jobs?category=software-dev&limit=100")
    results = []
    for item in data.get("jobs", []):
        results.append({"source": "Remotive", "source_id": str(item.get("id", "")), "title": item.get("title", ""), "company": item.get("company_name", ""), "location": item.get("candidate_required_location", "Remote"), "description": item.get("description", ""), "url": item.get("url", ""), "posted_at": item.get("publication_date")})
    return results


def fetch_remoteok() -> list[dict]:
    """Adapta o feed JSON do Remote OK ao formato comum do banco."""
    data = fetch_json("https://remoteok.com/api")
    results = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        results.append({"source": "Remote OK", "source_id": str(item.get("id")), "title": item.get("position", ""), "company": item.get("company", ""), "location": item.get("location", "Worldwide"), "description": item.get("description", ""), "url": item.get("url", ""), "posted_at": item.get("date")})
    return results


def fetch_adzuna() -> list[dict]:
    """Consulta Adzuna em países escolhidos; as credenciais vêm do cofre do Windows."""
    app_id, app_key = secret_get("adzuna_app_id"), secret_get("adzuna_app_key")
    if not app_id or not app_key:
        raise ValueError("Configure Adzuna app_id e app_key no painel.")
    cfg = settings()
    search_terms = terms(cfg.get("keywords", ""))[:2]
    countries = [code.strip().lower() for code in cfg.get("adzuna_countries", "br,us,gb,ca").split(",") if code.strip()]
    results = []
    for country in countries:
        for search in search_terms:
            query = f"https://api.adzuna.com/v1/api/jobs/{quote_plus(country)}/search/1?app_id={quote_plus(app_id)}&app_key={quote_plus(app_key)}&results_per_page=50&what={quote_plus(search)}&content-type=application/json"
            data = fetch_json(query)
            for item in data.get("results", []):
                results.append({"source": "Adzuna", "source_id": str(item.get("id", "")), "title": item.get("title", ""), "company": (item.get("company") or {}).get("display_name", ""), "location": (item.get("location") or {}).get("display_name", ""), "description": item.get("description", ""), "url": item.get("redirect_url", ""), "posted_at": item.get("created")})
    return results


APIFY_API = "https://api.apify.com/v2"
DEFAULT_APIFY_ACTORS: list[dict] = [
    {
        "id": "curious_coder~linkedin-jobs-scraper",
        "label": "LinkedIn Jobs",
        "enabled": True,
        "input_mode": "linkedin_search",
        "count": 25,
    }
]


def apify_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def apify_monthly_usage_usd(token: str) -> tuple[float, str]:
    """Consulta o uso do ciclo mensal atual da conta Apify (créditos em USD)."""
    payload = fetch_json(f"{APIFY_API}/users/me/usage/monthly", headers=apify_headers(token), timeout=30)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    used = float(data.get("totalUsageCreditsUsdAfterVolumeDiscount") or data.get("totalUsageCreditsUsdBeforeVolumeDiscount") or 0)
    cycle = data.get("usageCycle") or {}
    start = str(cycle.get("startAt", ""))[:10]
    end = str(cycle.get("endAt", ""))[:10]
    label = f"{start} a {end}" if start or end else "ciclo atual"
    return used, label


def normalize_apify_actor_id(actor_id: str) -> str:
    raw = (actor_id or "").strip()
    if "/" in raw and "~" not in raw:
        owner, name = raw.split("/", 1)
        return f"{owner.strip()}~{name.strip()}"
    return raw


def load_apify_actors(cfg: dict[str, str] | None = None) -> list[dict]:
    cfg = cfg or settings()
    raw = (cfg.get("apify_actors_json") or "").strip()
    if not raw:
        return [dict(item) for item in DEFAULT_APIFY_ACTORS]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON de actors Apify inválido: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError("apify_actors_json deve ser uma lista JSON com pelo menos um actor.")
    actors: list[dict] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Actor Apify #{idx + 1} deve ser um objeto JSON.")
        actor_id = normalize_apify_actor_id(str(item.get("id") or ""))
        if not actor_id:
            raise ValueError(f"Actor Apify #{idx + 1} sem id.")
        label = str(item.get("label") or actor_id).strip()
        enabled = bool(item.get("enabled", True))
        mode = str(item.get("input_mode") or "linkedin_search").strip().casefold()
        if mode not in {"linkedin_search", "custom"}:
            mode = "custom" if item.get("input_template") or item.get("input") else "linkedin_search"
        try:
            count = max(1, min(100, int(item.get("count") or cfg.get("apify_job_count", "25") or 25)))
        except (TypeError, ValueError):
            count = 25
        template = item.get("input_template") if "input_template" in item else item.get("input")
        actors.append(
            {
                "id": actor_id,
                "label": label,
                "enabled": enabled,
                "input_mode": mode,
                "count": count,
                "input_template": template if isinstance(template, (dict, list)) else None,
            }
        )
    return actors


def _substitute_apify_value(value: object, ctx: dict[str, str]) -> object:
    if isinstance(value, str):
        out = value
        for key, replacement in ctx.items():
            out = out.replace("{{" + key + "}}", replacement)
        return out
    if isinstance(value, list):
        return [_substitute_apify_value(item, ctx) for item in value]
    if isinstance(value, dict):
        return {str(k): _substitute_apify_value(v, ctx) for k, v in value.items()}
    return value


# LinkedIn Jobs search: f_E = experience, f_WT = workplace type, f_TPR = time posted.
LINKEDIN_EXPERIENCE_CODES = {1, 2, 3, 4, 5, 6}
LINKEDIN_WORKPLACE_CODES = {1, 2, 3}  # 1 on-site, 2 remote, 3 hybrid
#: Past 24 hours — máxima prioridade (vagas do mesmo dia).
LINKEDIN_TPR_DAY = "r86400"
#: Past week — teto duro de 7 dias (nunca buscar mais antigo que isso).
LINKEDIN_TPR_WEEK = "r604800"
LINKEDIN_MAX_AGE_DAYS = 7


def _resume_filter_fingerprint() -> str:
    with connect() as db:
        summaries = resume_summaries(db)
        rows = db.execute(
            "SELECT language, analyzed_at, file_sha256 FROM resumes ORDER BY language"
        ).fetchall()
    blob = json.dumps(
        {
            "summaries": summaries,
            "meta": [
                {
                    "language": r["language"],
                    "analyzed_at": r["analyzed_at"],
                    "sha": r["file_sha256"],
                }
                for r in rows
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _fallback_linkedin_filter(cfg: dict[str, str]) -> dict:
    keywords = terms(cfg.get("keywords", ""))[:4] or ["software engineer"]
    # Remoto global; híbrido só em Belo Horizonte (não priorizar Brasil/país inteiro).
    return {
        "keywords": keywords,
        "locations": ["Remote", "Belo Horizonte"],
        "experience_levels": [3, 4],
        "workplace_types": [2, 3],
        "reason": (
            "Fallback: keywords das preferências; locations Remote + Belo Horizonte; "
            "f_WT remote+hybrid (híbrido pensado para BH)."
        ),
        "source": "fallback",
    }


def _normalize_linkedin_filter(raw: dict, cfg: dict[str, str]) -> dict:
    fallback = _fallback_linkedin_filter(cfg)
    keywords = raw.get("keywords") if isinstance(raw.get("keywords"), list) else []
    keywords = [str(k).strip() for k in keywords if str(k).strip()][:5]
    if not keywords:
        keywords = list(fallback["keywords"])
    locations = raw.get("locations") if isinstance(raw.get("locations"), list) else []
    locations = [str(x).strip() for x in locations if str(x).strip()][:4]
    if not locations:
        locations = list(fallback["locations"])
    experience = []
    for item in raw.get("experience_levels") or []:
        try:
            code = int(item)
        except (TypeError, ValueError):
            continue
        if code in LINKEDIN_EXPERIENCE_CODES and code not in experience:
            experience.append(code)
    if not experience:
        experience = list(fallback["experience_levels"])
    workplace = []
    for item in raw.get("workplace_types") or []:
        try:
            code = int(item)
        except (TypeError, ValueError):
            continue
        if code in LINKEDIN_WORKPLACE_CODES and code not in workplace:
            workplace.append(code)
    if not workplace:
        workplace = list(fallback["workplace_types"])
    reason = str(raw.get("reason") or "").strip()[:400]
    return {
        "keywords": keywords,
        "locations": locations,
        "experience_levels": experience,
        "workplace_types": workplace,
        "reason": reason or fallback["reason"],
        "source": str(raw.get("source") or "ai"),
    }


def ai_generate_linkedin_search_filter(cfg: dict[str, str]) -> dict:
    """Pede à IA keywords + níveis LinkedIn (f_E/f_WT) com base no currículo analisado."""
    with connect() as db:
        summaries = resume_summaries(db)
    resume_pt = (summaries.get("pt") or "").strip()
    resume_en = (summaries.get("en") or "").strip()
    if not resume_pt and not resume_en:
        raise ValueError("Analise um currículo antes de gerar o filtro LinkedIn/Apify.")

    facts_pt = (cfg.get("candidate_facts_pt") or "").strip()
    facts_en = (cfg.get("candidate_facts_en") or "").strip()
    pref_keywords = cfg.get("keywords", "")
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    api_key = get_ai_key(provider)
    if not api_key:
        raise AiUnavailableError("Configure a chave da API de IA para gerar o filtro LinkedIn.")

    prompt = f"""You design LinkedIn Jobs search filters for Apify (curious_coder linkedin-jobs-scraper).
The scraper takes LinkedIn search URLs. Choose filters that match THIS candidate's real level and stack.

LinkedIn experience codes (f_E): 1 Internship, 2 Entry, 3 Associate, 4 Mid-Senior, 5 Director, 6 Executive.
LinkedIn workplace codes (f_WT): 1 On-site, 2 Remote, 3 Hybrid.

GEO / WORKPLACE RULES (mandatory — override panel location preferences):
- Do NOT prioritize Brazil, Brasil, or Brazilian cities as the main search target.
- Remote (f_WT=2) is the PRIMARY target: the candidate accepts remote work from ANYWHERE worldwide.
- For remote searches, prefer location strings like "Remote", "Worldwide", "United States", "Europe", or leave broad global remote — NOT "Brazil" as the default.
- Hybrid (f_WT=3) is allowed ONLY when paired with location "Belo Horizonte" (or "Belo Horizonte, Minas Gerais" / "Belo Horizonte, Brazil").
- Do NOT use f_WT=1 (On-site) unless the resume explicitly requires it (default: omit on-site).
- Typical good combo: workplace_types [2, 3] with locations including "Remote" and "Belo Horizonte" (hybrid applies to BH only in intent).
- Never make "Brazil" the only or primary location.

Other rules:
- Prefer 2–4 experience codes centered on the candidate's seniority (usually one level below + main; avoid Director/Executive unless clearly supported).
- keywords: 2–5 concrete job-search phrases (role + stack when useful), preferably in English for global remote reach; PT only if clearly Brazil-hybrid BH search.
- Do NOT invent employers or skills; only use the resume summaries and facts.
- RECENCY (mandatory — the URL builder enforces this; mention it in reason):
  * Only consider jobs from the last 7 days (LinkedIn f_TPR=r604800).
  * Strongly prioritize jobs posted today / last 24 hours (f_TPR=r86400) over older ones in the week.
- Return ONLY JSON with keys:
  keywords (string array),
  locations (string array),
  experience_levels (integer array of f_E codes),
  workplace_types (integer array of f_WT codes),
  reason (short string in Portuguese explaining the choice, mentioning remote global + hybrid BH + prioridade a vagas do dia / máx. 7 dias).

Panel keyword preferences (roles/skills only — ignore geo bias here): {pref_keywords or '[none]'}
Candidate facts PT: {facts_pt or '[none]'}
Candidate facts EN: {facts_en or '[none]'}
Resume summary PT: {resume_pt[:6000] or '[none]'}
Resume summary EN: {resume_en[:6000] or '[none]'}
"""
    try:
        if provider == "openai":
            endpoint = "https://api.openai.com/v1/responses"
            payload = json.dumps(
                {
                    "model": model,
                    "input": prompt,
                    "text": {"format": {"type": "json_object"}},
                    "store": False,
                    "max_output_tokens": 500,
                }
            ).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == "gemini":
            endpoint = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
            )
            payload = json.dumps(
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 500, "responseMimeType": "application/json"},
                }
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            raise ValueError("Provedor de IA inválido.")
        request = Request(endpoint, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code in {401, 403, 429} or "quota" in body.casefold():
            raise AiUnavailableError(f"IA indisponível ao gerar filtro LinkedIn (HTTP {exc.code}).") from exc
        raise
    except URLError as exc:
        raise AiUnavailableError(f"IA indisponível ao gerar filtro LinkedIn: {exc}") from exc

    if provider == "openai":
        raw_text = "\n".join(
            part.get("text", "")
            for item in result.get("output", [])
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
    else:
        raw_text = "\n".join(
            part.get("text", "")
            for item in result.get("candidates", [])
            for part in item.get("content", {}).get("parts", [])
        )
    decision = json.loads(raw_text.strip())
    if not isinstance(decision, dict):
        raise ValueError("Resposta da IA para filtro LinkedIn inválida.")
    normalized = _normalize_linkedin_filter(decision, cfg)
    normalized["source"] = "ai"
    return normalized


def get_linkedin_search_filter(cfg: dict[str, str], *, force_refresh: bool = False) -> dict:
    """Retorna filtro LinkedIn em cache ou gera com IA quando o currículo mudou."""
    fingerprint = _resume_filter_fingerprint()
    cached_hash = (cfg.get("apify_linkedin_filter_hash") or "").strip()
    cached_raw = (cfg.get("apify_linkedin_filter_json") or "").strip()
    if not force_refresh and cached_hash == fingerprint and cached_raw:
        try:
            data = json.loads(cached_raw)
            if isinstance(data, dict):
                out = _normalize_linkedin_filter(data, cfg)
                out["source"] = str(data.get("source") or "cache")
                return out
        except json.JSONDecodeError:
            pass

    try:
        generated = ai_generate_linkedin_search_filter(cfg)
    except (AiUnavailableError, ValueError, json.JSONDecodeError) as exc:
        log_event("warning", "apify", f"Filtro LinkedIn via IA indisponível ({exc}); usando fallback.")
        generated = _fallback_linkedin_filter(cfg)

    payload = dict(generated)
    set_setting("apify_linkedin_filter_json", json.dumps(payload, ensure_ascii=False))
    set_setting("apify_linkedin_filter_hash", fingerprint)
    log_event(
        "info",
        "apify",
        "Filtro LinkedIn "
        f"({payload.get('source')}): keywords={payload['keywords']}, "
        f"locations={payload['locations']}, f_E={payload['experience_levels']}, "
        f"f_WT={payload['workplace_types']}. {payload.get('reason', '')}",
    )
    return payload


def linkedin_filter_panel_html(cfg: dict[str, str] | None = None) -> str:
    """Bloco somente leitura do filtro LinkedIn em cache (gerado pela IA)."""
    cfg = cfg or settings()
    raw = (cfg.get("apify_linkedin_filter_json") or "").strip()
    if not raw:
        body = (
            '<p class="hint" style="margin:0">Ainda não há filtro gerado. Ele aparece após a primeira '
            "coleta Apify em modo <code>linkedin_search</code> (com currículo já analisado).</p>"
        )
        return (
            '<div id="linkedin-filter-panel" class="resume-card" style="margin-top:12px">'
            "<h3 style=\"margin:0 0 8px;font-size:14px\">Filtro LinkedIn (IA)</h3>"
            f"{body}</div>"
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    exp_labels = {
        1: "Internship",
        2: "Entry",
        3: "Associate",
        4: "Mid-Senior",
        5: "Director",
        6: "Executive",
    }
    wt_labels = {1: "On-site", 2: "Remote", 3: "Hybrid"}
    keywords = data.get("keywords") if isinstance(data.get("keywords"), list) else []
    locations = data.get("locations") if isinstance(data.get("locations"), list) else []
    experience = data.get("experience_levels") if isinstance(data.get("experience_levels"), list) else []
    workplace = data.get("workplace_types") if isinstance(data.get("workplace_types"), list) else []
    source = esc(str(data.get("source") or "cache"))
    reason = esc(str(data.get("reason") or "").strip())
    exp_bits = []
    for x in experience:
        try:
            code = int(x)
        except (TypeError, ValueError):
            continue
        exp_bits.append(f"{exp_labels.get(code, code)} ({code})")
    wt_bits = []
    for x in workplace:
        try:
            code = int(x)
        except (TypeError, ValueError):
            continue
        wt_bits.append(f"{wt_labels.get(code, code)} ({code})")
    exp_txt = ", ".join(exp_bits) or "—"
    wt_txt = ", ".join(wt_bits) or "—"
    pretty = esc(json.dumps(data, ensure_ascii=False, indent=2))
    return (
        '<div id="linkedin-filter-panel" class="resume-card" style="margin-top:12px">'
        '<h3 style="margin:0 0 8px;font-size:14px">Filtro LinkedIn (IA)</h3>'
        f'<p class="hint" style="margin:0 0 8px"><strong>Origem:</strong> {source}<br>'
        f"<strong>Keywords:</strong> {esc(', '.join(str(k) for k in keywords) or '—')}<br>"
        f"<strong>Locations:</strong> {esc(', '.join(str(x) for x in locations) or '—')}<br>"
        f"<strong>Nível (f_E):</strong> {esc(exp_txt)}<br>"
        f"<strong>Local de trabalho (f_WT):</strong> {esc(wt_txt)}<br>"
        f"<strong>Recência (f_TPR):</strong> máx. {LINKEDIN_MAX_AGE_DAYS} dias "
        f"(prioridade: mesmo dia / 24h)</p>"
        + (f'<p class="hint" style="margin:0 0 8px"><strong>Motivo:</strong> {reason}</p>' if reason else "")
        + '<details><summary>JSON completo</summary>'
        f'<pre style="margin:8px 0 0;white-space:pre-wrap;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;'
        f'max-height:240px;overflow:auto;background:#f4f7f7;padding:10px;border-radius:8px">{pretty}</pre>'
        "</details>"
        '<p class="hint" style="margin:8px 0 0">Somente leitura. As URLs de busca sempre usam '
        f"<code>f_TPR={LINKEDIN_TPR_DAY}</code> (mesmo dia, prioridade) e "
        f"<code>f_TPR={LINKEDIN_TPR_WEEK}</code> (≤{LINKEDIN_MAX_AGE_DAYS} dias). "
        "Regenera automaticamente quando o currículo analisado muda e roda uma nova coleta LinkedIn/Apify.</p>"
        "</div>"
    )


def build_linkedin_search_urls(filter_data: dict, *, max_urls: int = 4) -> list[str]:
    """Monta URLs de busca LinkedIn com recência obrigatória.

    Toda URL leva ``f_TPR``: metade (arredondando pra cima) prioriza o mesmo dia
    (``r86400``); o restante completa a janela de 7 dias (``r604800``). Nunca
    busca sem teto de tempo — evita vagas velhas no scrape.
    """
    keywords = [str(k) for k in (filter_data.get("keywords") or []) if str(k).strip()][:4]
    locations = [str(x) for x in (filter_data.get("locations") or []) if str(x).strip()][:4]
    if not keywords:
        keywords = ["software engineer"]
    if not locations:
        locations = ["Remote", "Belo Horizonte"]
    experience = [int(x) for x in (filter_data.get("experience_levels") or []) if int(x) in LINKEDIN_EXPERIENCE_CODES]
    workplace = [int(x) for x in (filter_data.get("workplace_types") or []) if int(x) in LINKEDIN_WORKPLACE_CODES]
    f_e = "%2C".join(str(x) for x in experience) if experience else ""

    def _is_bh(loc: str) -> bool:
        low = loc.casefold()
        return "belo horizonte" in low or low in {"bh", "bh, mg", "bh - mg"}

    def _is_remote_loc(loc: str) -> bool:
        low = loc.casefold()
        return any(
            token in low
            for token in ("remote", "remoto", "worldwide", "anywhere", "global", "europe", "united states", "usa", "eua")
        )

    # Pares intencionais: remote → qualquer lugar remoto; hybrid → só BH.
    pairs: list[tuple[str, str]] = []
    if 2 in workplace:
        remote_locs = [loc for loc in locations if _is_remote_loc(loc)] or ["Remote"]
        for loc in remote_locs[:2]:
            pairs.append((loc, "2"))
    if 3 in workplace:
        bh_locs = [loc for loc in locations if _is_bh(loc)] or ["Belo Horizonte"]
        for loc in bh_locs[:1]:
            pairs.append((loc, "3"))
    if 1 in workplace and not pairs:
        pairs.append((locations[0], "1"))
    if not pairs:
        # Sem f_WT explícito: remote global + BH.
        pairs = [("Remote", "2"), ("Belo Horizonte", "3")]

    def _url(keyword: str, location: str, f_wt: str, tpr: str) -> str:
        url = (
            f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(keyword)}"
            f"&location={quote_plus(location)}&position=1&pageNum=0"
            f"&f_TPR={tpr}"
        )
        if f_e:
            url += f"&f_E={f_e}"
        if f_wt:
            url += f"&f_WT={f_wt}"
        return url

    day_urls: list[str] = []
    week_urls: list[str] = []
    for keyword in keywords:
        for location, f_wt in pairs:
            day_urls.append(_url(keyword, location, f_wt, LINKEDIN_TPR_DAY))
            week_urls.append(_url(keyword, location, f_wt, LINKEDIN_TPR_WEEK))

    # Prioridade: mesmo dia primeiro (≥ metade dos slots); resto ≤7 dias.
    day_slots = max(1, (max_urls + 1) // 2)
    urls: list[str] = []
    for u in day_urls[:day_slots]:
        if u not in urls:
            urls.append(u)
        if len(urls) >= max_urls:
            return urls
    for u in week_urls:
        if u not in urls:
            urls.append(u)
        if len(urls) >= max_urls:
            return urls
    for u in day_urls[day_slots:]:
        if u not in urls:
            urls.append(u)
        if len(urls) >= max_urls:
            return urls
    return urls


def build_apify_run_input(actor: dict, cfg: dict[str, str]) -> dict:
    keywords = terms(cfg.get("keywords", ""))[:3] or ["software engineer"]
    locations = terms(cfg.get("locations", ""))[:3] or ["remote"]
    count = int(actor.get("count") or 25)
    mode = str(actor.get("input_mode") or "linkedin_search")
    if mode == "linkedin_search":
        filter_data = get_linkedin_search_filter(cfg)
        urls = build_linkedin_search_urls(filter_data, max_urls=4)
        return {"urls": urls, "count": count, "scrapeCompany": False}

    template = actor.get("input_template")
    if not isinstance(template, (dict, list)):
        raise ValueError(
            f"Actor {actor.get('label')} em modo custom precisa de input_template (objeto JSON)."
        )
    # Prefer AI LinkedIn filter keywords when available for template placeholders.
    try:
        linkedin_filter = get_linkedin_search_filter(cfg)
        keywords = list(linkedin_filter.get("keywords") or keywords)[:3] or keywords
        locations = list(linkedin_filter.get("locations") or locations)[:3] or locations
    except Exception:
        pass
    ctx = {
        "count": str(count),
        "keywords": ", ".join(keywords),
        "locations": ", ".join(locations),
        "keyword": keywords[0],
        "location": locations[0],
        "keyword0": keywords[0],
        "keyword1": keywords[1] if len(keywords) > 1 else keywords[0],
        "location0": locations[0],
        "location1": locations[1] if len(locations) > 1 else locations[0],
    }
    built = _substitute_apify_value(template, ctx)
    if isinstance(built, list):
        return {"items": built}
    if not isinstance(built, dict):
        raise ValueError(f"input_template do actor {actor.get('label')} deve resultar em objeto JSON.")
    return built


def _apify_item_url(item: dict) -> str:
    """Extrai URL aplicável de payloads LinkedIn/Indeed (incl. viewJobLink relativo)."""
    candidates = [
        item.get("originalApplyUrl"),
        item.get("link"),
        item.get("url"),
        item.get("applyUrl"),
        item.get("jobUrl"),
        item.get("externalApplyLink"),
        item.get("viewJobLink"),
        item.get("jobLink"),
    ]
    for raw in candidates:
        url = str(raw or "").strip()
        if not url:
            continue
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/"):
            base = ""
            for key in ("companyOverviewLink", "companyUrl", "url", "link"):
                hint = str(item.get(key) or "").strip()
                if hint.startswith("http://") or hint.startswith("https://"):
                    parsed = urlparse(hint)
                    if parsed.scheme and parsed.netloc:
                        base = f"{parsed.scheme}://{parsed.netloc}"
                        break
            if not base:
                # Indeed default quando só vem path relativo.
                base = "https://www.indeed.com"
            return base.rstrip("/") + url
    return ""


def _linkedin_posted_age_days(posted_at: object) -> float | None:
    """Idade em dias da postagem, ou None se não der pra inferir.

    Aceita ISO / timestamp e textos relativos comuns do LinkedIn
    ('Just now', '2 hours ago', '3 days ago', '1 week ago', ...).
    """
    if posted_at is None or posted_at == "":
        return None
    if isinstance(posted_at, (int, float)):
        ts = float(posted_at)
        if ts > 10_000_000_000:
            ts /= 1000.0
        try:
            when = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400.0)

    raw = str(posted_at).strip()
    parsed = parse_utc(raw)
    if parsed is not None:
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)

    low = raw.casefold()
    if any(tok in low for tok in ("just now", "agora", "moments ago", "seconds ago", "second ago")):
        return 0.0
    m = re.search(r"(\d+)\s*(minute|minutos?|hour|horas?|day|dias?|week|semanas?|month|meses?)", low)
    if not m:
        m2 = re.search(r"(\d+)\s*([mhdw])\b", low)
        if not m2:
            return None
        n = int(m2.group(1))
        unit = m2.group(2)
        if unit == "m":
            return n / (60 * 24)
        if unit == "h":
            return n / 24.0
        if unit == "d":
            return float(n)
        if unit == "w":
            return float(n * 7)
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("min"):
        return n / (60 * 24)
    if unit.startswith("hour") or unit.startswith("hora"):
        return n / 24.0
    if unit.startswith("day") or unit.startswith("dia"):
        return float(n)
    if unit.startswith("week") or unit.startswith("semana"):
        return float(n * 7)
    if unit.startswith("month") or unit.startswith("mes"):
        return float(n * 30)
    return None


def _linkedin_within_max_age(posted_at: object, *, max_days: int = LINKEDIN_MAX_AGE_DAYS) -> bool:
    """True se a vaga cabe na janela (ou a data é desconhecida — não descartamos)."""
    age = _linkedin_posted_age_days(posted_at)
    if age is None:
        return True
    return age <= float(max_days)


def normalize_apify_items(items: list, *, label: str) -> list[dict]:
    results: list[dict] = []
    source_name = f"Apify:{label}" if label else "Apify"
    is_linkedin = "linkedin" in (label or "").casefold()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = _apify_item_url(item)
        title = str(item.get("title") or item.get("position") or item.get("jobTitle") or item.get("displayTitle") or "").strip()
        if not url or not title:
            continue
        description = str(
            item.get("descriptionText")
            or item.get("descriptionHtml")
            or item.get("description")
            or item.get("jobDescription")
            or item.get("jobDescriptionHTML")
            or ""
        )
        company = item.get("companyName") or item.get("company") or item.get("companyDetails") or item.get("jobSourceName") or {}
        if isinstance(company, dict):
            company = company.get("name") or company.get("display_name") or ""
        location = (
            item.get("formattedLocation")
            or item.get("location")
            or item.get("jobLocation")
            or ""
        )
        if isinstance(location, dict):
            formatted = location.get("formatted") or {}
            if isinstance(formatted, dict):
                location = (
                    formatted.get("long")
                    or formatted.get("short")
                    or location.get("fullAddress")
                    or location.get("display_name")
                    or location.get("name")
                    or ""
                )
            else:
                location = (
                    location.get("fullAddress")
                    or location.get("display_name")
                    or location.get("name")
                    or ""
                )
        if not location:
            city = str(item.get("jobLocationCity") or "").strip()
            state = str(item.get("jobLocationState") or "").strip()
            location = ", ".join(p for p in (city, state) if p)
        posted_at = item.get("postedAt") or item.get("publishedAt") or item.get("date") or item.get("pubDate")
        if isinstance(posted_at, (int, float)) and posted_at > 10_000_000_000:
            # Indeed pubDate em milissegundos.
            posted_at = datetime.fromtimestamp(posted_at / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        if is_linkedin and not _linkedin_within_max_age(posted_at):
            continue  # teto duro: >7 dias fora
        results.append(
            {
                "source": source_name,
                "source_id": str(item.get("id") or item.get("jobId") or url),
                "title": title,
                "company": str(company or ""),
                "location": str(location or ""),
                "description": description,
                "url": url,
                "posted_at": posted_at,
            }
        )
    if is_linkedin and results:
        # Prioriza o mais recente (mesmo dia primeiro) na fila de inserção/triagem.
        def _sort_key(job: dict) -> tuple:
            age = _linkedin_posted_age_days(job.get("posted_at"))
            return (age if age is not None else 3.5, str(job.get("title") or ""))

        results.sort(key=_sort_key)
    return results


def run_apify_actor(token: str, actor: dict, cfg: dict[str, str]) -> list[dict]:
    actor_id = normalize_apify_actor_id(str(actor["id"]))
    label = str(actor.get("label") or actor_id)
    run_input = build_apify_run_input(actor, cfg)
    log_event("info", "apify", f"Iniciando actor {label} ({actor_id}).")
    started = fetch_json(
        f"{APIFY_API}/actors/{quote_plus(actor_id)}/runs",
        data=json.dumps(run_input).encode("utf-8"),
        headers=apify_headers(token),
        timeout=45,
    )
    run = started.get("data", started) if isinstance(started, dict) else {}
    run_id = str(run.get("id") or "")
    dataset_id = str(run.get("defaultDatasetId") or "")
    if not run_id:
        raise RuntimeError(f"Apify ({label}) não retornou o identificador da execução.")
    deadline = time.time() + 180
    status = str(run.get("status") or "READY")
    while status in {"READY", "RUNNING"} and time.time() < deadline:
        if collector.stop_event.is_set():
            raise RuntimeError(f"Coleta interrompida durante Apify ({label}).")
        time.sleep(5)
        progress = fetch_json(f"{APIFY_API}/actor-runs/{quote_plus(run_id)}", headers=apify_headers(token), timeout=30)
        run = progress.get("data", progress) if isinstance(progress, dict) else {}
        status = str(run.get("status") or status)
        dataset_id = str(run.get("defaultDatasetId") or dataset_id)
    if status != "SUCCEEDED":
        raise RuntimeError(f"Apify ({label}) encerrada com status {status}.")
    if not dataset_id:
        raise RuntimeError(f"Apify ({label}) não produziu dataset.")
    items = fetch_json(f"{APIFY_API}/datasets/{quote_plus(dataset_id)}/items?clean=1", headers=apify_headers(token), timeout=45)
    if not isinstance(items, list):
        items = []
    normalized = normalize_apify_items(items, label=label)
    log_event("success", "apify", f"Actor {label}: {len(normalized)} vaga(s) úteis.")
    return normalized


def fetch_apify() -> list[dict]:
    """Roda todos os actors Apify habilitados e mescla os resultados."""
    token = secret_get("apify_token")
    if not token:
        raise ValueError("Configure o token da Apify no painel.")
    cfg = settings()
    try:
        limit = float(str(cfg.get("apify_monthly_credit_limit_usd", "5")).replace(",", "."))
    except ValueError:
        limit = 5.0
    if limit <= 0:
        raise ValueError("Limite mensal de créditos Apify está em zero; a fonte não será consultada.")
    used, cycle_label = apify_monthly_usage_usd(token)
    set_setting("apify_last_usage_usd", f"{used:.4f}")
    set_setting("apify_last_usage_cycle", cycle_label)
    if used >= limit:
        raise ValueError(f"Limite mensal de créditos Apify atingido ({used:.2f} USD de {limit:.2f} USD no ciclo {cycle_label}).")

    actors = [actor for actor in load_apify_actors(cfg) if actor.get("enabled", True)]
    if not actors:
        raise ValueError("Nenhum actor Apify habilitado em apify_actors_json.")

    results: list[dict] = []
    errors: list[str] = []
    for actor in actors:
        if collector.stop_event.is_set():
            break
        # Re-check credit budget between actors.
        try:
            used_now, _ = apify_monthly_usage_usd(token)
            if used_now >= limit:
                errors.append(f"{actor.get('label')}: limite de créditos atingido antes da execução")
                break
        except Exception:
            pass
        try:
            results.extend(run_apify_actor(token, actor, cfg))
        except Exception as exc:
            LOG.exception("Falha no actor Apify %s", actor.get("id"))
            errors.append(f"{actor.get('label')}: {exc}")
            log_event("error", "apify", f"Falha no actor {actor.get('label')}: {exc}")
    try:
        used_after, cycle_after = apify_monthly_usage_usd(token)
        set_setting("apify_last_usage_usd", f"{used_after:.4f}")
        set_setting("apify_last_usage_cycle", cycle_after)
    except Exception:
        pass
    if errors and not results:
        raise RuntimeError("Todos os actors Apify falharam: " + "; ".join(errors))
    if errors:
        log_event("warning", "apify", "Alguns actors falharam: " + "; ".join(errors))
    return results


PROVIDERS = {"remotive": fetch_remotive, "remoteok": fetch_remoteok, "adzuna": fetch_adzuna, "apify": fetch_apify}


class Collector:
    """Executa consultas em segundo plano e permite iniciar/parar pelo painel."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "stopped"
        self.message = "Coleta parada"
        self.last_run: str | None = None
        self.last_found = 0
        self.next_run_at: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "message": self.message,
                "last_run": self.last_run,
                "last_found": self.last_found,
                "next_run_at": self.next_run_at,
            }

    def start(self) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self.stop_event.clear()
            self.state = "running"
            self.message = "Iniciando busca…"
            self.next_run_at = None
            self.thread = threading.Thread(target=self._loop, daemon=True, name="job-collector")
            self.thread.start()
            log_event("success", "collector", "Bot de coleta iniciado.")
            return True

    def _set_progress(self, message: str, found: int | None = None) -> None:
        with self.lock:
            self.message = message
            if found is not None:
                self.last_found = found

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.state == "running":
                self.state = "stopping"
                self.message = "Parando após a busca atual…"
                log_event("info", "collector", "Parada solicitada; aguardando fim da busca atual.")

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                self.next_run_at = None
            self._run_once()
            if self.stop_event.is_set():
                break
            cfg = settings()
            try:
                seconds = max(1, int(cfg.get("interval_minutes", "15"))) * 60
            except ValueError:
                seconds = POLL_SECONDS
            next_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            with self.lock:
                self.next_run_at = next_at.isoformat(timespec="seconds")
                mins = seconds // 60
                secs = seconds % 60
                wait_label = f"{mins} min" if secs == 0 else f"{mins} min {secs}s"
                self.message = f"Aguardando próxima busca ({wait_label})…"
            if self.stop_event.wait(seconds):
                break
        with self.lock:
            self.state = "stopped"
            self.message = "Coleta parada"
            self.next_run_at = None
        log_event("info", "collector", "Coleta parada.")

    def _run_once(self) -> None:
        started = now_iso()
        log_event("info", "collector", "Início do ciclo de busca.")
        with connect() as db:
            cursor = db.execute("INSERT INTO runs(started_at,state) VALUES(?, 'running')", (started,))
            run_id = cursor.lastrowid
        cfg = settings()
        keyword_terms = terms(cfg.get("keywords", ""))
        location_terms = terms(cfg.get("locations", ""))
        sources = [value.strip().casefold() for value in cfg.get("sources", "").split(",") if value.strip()]
        found = 0
        errors: list[str] = []
        self._set_progress("Buscando vagas…", 0)
        for source in sources:
            if self.stop_event.is_set():
                break
            provider = PROVIDERS.get(source)
            if not provider:
                errors.append(f"Fonte desconhecida: {source}")
                continue
            self._set_progress(f"Consultando {source}… {found} vaga(s) nova(s) nesta execução.", found)
            log_event("info", "collector", f"Consultando fonte {source}…")
            try:
                jobs = provider()
                log_event("info", "collector", f"{source}: {len(jobs)} anúncio(s) recebidos antes dos filtros.")
                for job in jobs:
                    if self.stop_event.is_set():
                        break
                    searchable = " ".join((job["title"], job["company"], job["location"], re.sub("<[^>]+>", " ", job["description"]))).casefold()
                    if keyword_terms and not any(term in searchable for term in keyword_terms):
                        continue
                    if location_terms and not any(term in (job["location"] + " " + searchable).casefold() for term in location_terms):
                        continue
                    if not job["url"] or not job["title"]:
                        continue
                    language, language_confidence = detect_language(job["title"] + "\n" + re.sub(r"<[^>]+>", " ", job["description"]))
                    with connect() as db:
                        cursor = db.execute("""INSERT OR IGNORE INTO jobs(source,source_id,title,company,location,description,url,posted_at,first_seen_at,fingerprint,language,language_confidence)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (job["source"], job["source_id"], job["title"], job["company"], job["location"], job["description"], job["url"], job["posted_at"],  now_iso(), fingerprint(job), language, language_confidence))
                        inserted = max(cursor.rowcount, 0)
                    found += inserted
                    if inserted:
                        self._set_progress(f"Consultando {source}… {found} vaga(s) nova(s) nesta execução.", found)
            except Exception as exc:  # keep other sources running if one is unavailable
                LOG.exception("Falha ao consultar %s", source)
                errors.append(f"{source}: {exc.__class__.__name__}")
                log_event("error", "collector", f"Falha ao consultar {source}: {exc}")
        apply_note = ""
        if not self.stop_event.is_set() and settings().get("auto_apply") == "1":
            self._set_progress(f"Triagem/candidatura automática… {found} vaga(s) nova(s).", found)
            log_event("info", "auto-apply", "Iniciando triagem/candidatura automática do ciclo.")
            apply_note = process_auto_apply_batch()
            log_event("info", "auto-apply", apply_note or "Batch concluído sem mensagens.")
        elif not self.stop_event.is_set():
            log_event("info", "auto-apply", "auto_apply desligado; triagem automática ignorada neste ciclo.")
        finished = now_iso()
        message = f"Busca concluída: {found} vaga(s) nova(s)." + (" Avisos: " + "; ".join(errors) if errors else "")
        if apply_note:
            message += f" Auto-apply: {apply_note}"
        log_event("success" if not errors else "warning", "collector", message)
        with connect() as db:
            db.execute("UPDATE runs SET finished_at=?,state=?,found_count=?,message=? WHERE id=?", (finished, "completed" if not self.stop_event.is_set() else "stopped", found, message, run_id))
        with self.lock:
            self.last_run, self.last_found, self.message = finished, found, message


collector = Collector()


def _boot_logging() -> None:
    ensure_log_table()
    attach_to_logger()
    log_event("info", "app", "Painel de logs ativo.")


def _boot_queue() -> None:
    queue.start()


_boot_logging()
_boot_queue()


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def load_dashboard() -> tuple[dict, list, list]:
    with connect() as db:
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}
        jobs = db.execute("SELECT jobs.*, cover_letters.body AS cover_letter FROM jobs LEFT JOIN cover_letters ON cover_letters.job_id=jobs.id ORDER BY first_seen_at DESC LIMIT 250").fetchall()
        runs = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8").fetchall()
    return counts, jobs, runs


def stats_html(counts: dict) -> str:
    return "".join(
        f'<div class="stat"><span>{esc(label)}</span><strong>{counts.get(key, 0)}</strong></div>'
        for key, label in [
            ("new", "Novas"),
            ("worth", "Vale olhar"),
            ("prepared", "Preparadas"),
            ("applied", "Aplicadas"),
            ("ignored", "Ignoradas"),
        ]
    )


def job_rows_html(jobs, collecting: bool) -> str:
    rows = []
    for job in jobs:
        description = re.sub(r"<[^>]+>", " ", job["description"])
        lang_label = {"pt": "Português", "en": "English", "unknown": "Incerto"}.get(job["language"], job["language"])
        confidence = f" · {job['language_confidence']:.0%}" if job["language_confidence"] else ""
        letter_ui = f'<details><summary>{"Carta criada" if job["cover_letter"] else "Carta não criada"}</summary><div class="description">{esc(job["cover_letter"] or "A carta será gerada automaticamente na candidatura.")}</div></details>'
        if "COPILOT_UNAUTOMATED:" in (job["notes"] or ""):
            notes = (
                '<span style="background:#3a2f00;border:1px solid #e8c33f;color:#ffd54a;'
                'font-weight:700;padding:3px 8px;border-radius:6px">⚠ fluxo não automatizado pela IA'
                "</span>"
                f'<details><summary>Detalhes</summary><div class="description">{esc(job["notes"])}</div></details>'
            )
        else:
            notes = f'<details><summary>Notas</summary><div class="description">{esc(job["notes"] or "—")}</div></details>' if job["notes"] else ""
        rows.append(f'''<tr><td><a class="job-title" href="{esc(job['url'])}" target="_blank" rel="noreferrer">{esc(job['title'])}</a><small>{esc(job['company'])}</small></td><td>{esc(job['location'])}</td><td><span class="source" title="{esc(job['source'])}">{esc(job['source'])}</span></td><td><span title="Confiança do detector: {confidence}">{esc(lang_label + confidence)}</span></td><td>{esc(STATUSES.get(job['status'], job['status']))}</td><td>{letter_ui}{notes}<details><summary>Descrição</summary><div class="description">{esc(description[:1800])}</div></details></td><td><small>{esc(format_brasilia_date(job['posted_at'] or job['first_seen_at']))}</small></td></tr>''')
    if rows:
        return "".join(rows)
    if collecting:
        return '<tr><td colspan="7">Buscando vagas… as novas aparecem aqui assim que forem encontradas.</td></tr>'
    return '<tr><td colspan="7">Nenhuma vaga ainda. Configure as fontes e inicie o bot.</td></tr>'


def history_html(runs) -> str:
    return "".join(
        f'<li><span>{esc(format_brasilia(run["started_at"]))}</span> {esc(run["message"] or run["state"])}</li>'
        for run in runs
    ) or "<li>Nenhuma execução ainda.</li>"


def logs_html(limit: int = 200) -> str:
    rows = list_logs(limit)
    if not rows:
        return '<div class="log-empty">Nenhum evento ainda. Inicie o bot, envie um currículo ou rode um teste SMTP para ver o fluxo aqui.</div>'
    parts = []
    for row in rows:
        level = esc(row["level"])
        when = esc(format_brasilia(row["created_at"]))
        source = esc(row["source"])
        message = esc(row["message"])
        parts.append(
            f'<div class="log-line log-{level}">'
            f'<span class="log-time">{when}</span>'
            f'<span class="log-level">{level}</span>'
            f'<span class="log-source">{source}</span>'
            f'<span class="log-msg">{message}</span></div>'
        )
    return "".join(parts)


def queue_html(limit: int = 100) -> str:
    rows = queue.list_jobs(limit=limit)
    counts = queue.counts()
    summary = (
        f'<p class="hint">Ativos: pending={counts.get("pending",0)} · running={counts.get("running",0)} · '
        f'retry_wait={counts.get("retry_wait",0)} · succeeded={counts.get("succeeded",0)} · '
        f'failed={counts.get("failed",0)} · cancelled={counts.get("cancelled",0)}. '
        f'Todas as vagas novas entram na fila; paralelismo: {esc(settings().get("queue_max_workers","3"))} worker(s).</p>'
    )
    if not rows:
        return summary + '<p class="hint">Nenhum job na fila ainda.</p>'
    kind_label = {
        KIND_RESUME: "Análise de currículo",
        KIND_APPLY: "Candidatura",
        KIND_LINKEDIN: "Easy Apply LinkedIn",
    }
    status_label = {
        "pending": "Pendente",
        "running": "Executando",
        "retry_wait": "Aguardando retry",
        "succeeded": "Sucesso",
        "failed": "Falhou",
        "cancelled": "Cancelado",
    }
    body = []
    for row in rows:
        jid = int(row["id"])
        actions = []
        if row["status"] in {"pending", "retry_wait", "running"}:
            actions.append(
                f'<form method="post" action="/queue-cancel" class="js-process-form" style="display:inline">'
                f'<input type="hidden" name="id" value="{jid}"><button class="subtle" type="submit">Cancelar</button></form>'
            )
        if row["status"] in {"retry_wait", "failed", "cancelled", "running"}:
            actions.append(
                f'<form method="post" action="/queue-retry" class="js-process-form" style="display:inline">'
                f'<input type="hidden" name="id" value="{jid}"><button class="subtle" type="submit">Reenfileirar</button></form>'
            )
        body.append(
            "<tr>"
            f"<td>#{jid}</td>"
            f"<td>{esc(kind_label.get(row['kind'], row['kind']))}</td>"
            f"<td><span class=\"queue-status queue-{esc(row['status'])}\">{esc(status_label.get(row['status'], row['status']))}</span></td>"
            f"<td>{int(row['attempts'])}/{int(row['max_attempts'])}</td>"
            f"<td><small>{esc(format_brasilia(row['next_run_at']))}</small></td>"
            f"<td><small>{esc((row['last_error'] or row['result'] or '—')[:220])}</small></td>"
            f"<td>{' '.join(actions) or '—'}</td>"
            "</tr>"
        )
    table = (
        '<table style="min-width:100%;font-size:13px"><thead><tr>'
        "<th>ID</th><th>Tipo</th><th>Status</th><th>Tentativas</th><th>Próxima execução</th><th>Detalhe</th><th>Ações</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )
    clear_btn = (
        '<div style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap">'
        '<form method="post" action="/queue-eval-new" class="js-process-form">'
        '<button class="subtle" type="submit">Enfileirar vagas novas para triagem</button></form>'
        '<form method="post" action="/queue-retry-all" class="js-process-form">'
        '<button class="subtle" type="submit">Reenfileirar falhos/cancelados/retry</button></form>'
        '<form method="post" action="/queue-clear" class="js-process-form">'
        '<button class="subtle" type="submit">Limpar concluídos/falhos/cancelados</button></form>'
        "</div>"
    )
    return summary + table + clear_btn


WORTH_PAGE_SIZE = 15


def _worth_match_expr() -> str:
    return """(
                        SELECT match_score FROM ai_decisions
                        WHERE ai_decisions.job_id = jobs.id
                        ORDER BY id DESC LIMIT 1
                      )"""


def _notes_html(notes: str) -> str:
    """Nota do card 'Vale a pena olhar'; vermelho p/ handler ausente, AMARELO
    destacado p/ fluxo nao automatizado pelo copiloto de IA."""
    text = notes or ""
    if "COPILOT_UNAUTOMATED:" in text:
        return (
            '<p class="hint" style="background:#3a2f00;border:1px solid #e8c33f;'
            'color:#ffd54a;font-weight:700;padding:10px 12px;border-radius:8px">'
            "⚠ " + esc(text).replace("COPILOT_UNAUTOMATED: ", "Não foi possível automatizar o fluxo. ")
            + "</p>"
        )
    if "NO_HANDLER:" in text:
        return (
            '<p class="hint" style="color:#b3261e;font-weight:600">'
            f"{esc(text)}</p>"
        )
    return f'<p class="hint">{esc(text)}</p>'


def worth_html(*, page: int = 1, page_size: int = WORTH_PAGE_SIZE, min_match: int = 0) -> str:
    page = max(1, int(page or 1))
    page_size = max(5, min(50, int(page_size or WORTH_PAGE_SIZE)))
    min_match = max(0, min(100, int(min_match or 0)))
    offset = (page - 1) * page_size
    match_expr = _worth_match_expr()
    where = "jobs.status = 'worth'"
    params: list[object] = []
    if min_match > 0:
        where += f" AND COALESCE({match_expr}, 0) >= ?"
        params.append(min_match)

    with connect() as db:
        total = int(
            db.execute(
                f"SELECT COUNT(*) AS n FROM jobs WHERE {where}",
                params,
            ).fetchone()["n"]
        )
        rows = db.execute(
            f"""SELECT jobs.*, cover_letters.body AS cover_letter,
                      {match_expr} AS match_score
               FROM jobs
               LEFT JOIN cover_letters ON cover_letters.job_id = jobs.id
               WHERE {where}
               ORDER BY COALESCE(match_score, 0) DESC, jobs.id DESC
               LIMIT ? OFFSET ?""",
            [*params, page_size, offset],
        ).fetchall()

    filter_bar = (
        '<div class="worth-filters actions" style="margin:0 0 12px;align-items:flex-end;flex-wrap:wrap">'
        '<label style="margin:0">Match mínimo (%)'
        f'<input type="number" id="worth-min-match" min="0" max="100" step="1" value="{min_match}" '
        'style="width:88px;margin-top:4px"></label>'
        '<button type="button" class="subtle" id="worth-apply-filter">Filtrar</button>'
        "".join(
            f'<button type="button" class="subtle worth-match-preset" data-worth-min-match="{n}">'
            f'{"Todos" if n == 0 else f"{n}+"}</button>'
            for n in (0, 70, 80, 90)
        )
        + '<span class="worth-ignore-all-wrap" style="margin-left:auto">'
        '<button type="button" class="subtle" id="worth-ignore-all">Ignorar todas</button>'
        '<span id="worth-ignore-all-confirm" hidden style="display:none;align-items:center;gap:8px;flex-wrap:wrap">'
        '<span class="hint" style="margin:0;color:#ffd54a">Tem certeza? Isso ignora todas as vagas em Vale a pena olhar.</span>'
        '<button type="button" class="stop" id="worth-ignore-all-yes">Confirmar</button>'
        '<button type="button" class="subtle" id="worth-ignore-all-no">Cancelar</button>'
        "</span></span>"
        + "</div>"
    )

    if total == 0:
        empty = (
            '<div data-worth-current="1">'
            '<p class="hint">Nenhuma vaga neste filtro. '
            + (
                "Ajuste o match mínimo ou limpe o filtro."
                if min_match > 0
                else "Quando houver bom match mas LinkedIn, falha de formulário/e-mail, a vaga aparece aqui."
            )
            + "</p></div>"
        )
        return filter_bar + empty

    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        page = pages
        offset = (page - 1) * page_size
        with connect() as db:
            rows = db.execute(
                f"""SELECT jobs.*, cover_letters.body AS cover_letter,
                          {match_expr} AS match_score
                   FROM jobs
                   LEFT JOIN cover_letters ON cover_letters.job_id = jobs.id
                   WHERE {where}
                   ORDER BY COALESCE(match_score, 0) DESC, jobs.id DESC
                   LIMIT ? OFFSET ?""",
                [*params, page_size, offset],
            ).fetchall()

    cards = []
    cfg = settings()
    easy_on = cfg.get("linkedin_easy_apply") == "1" and cfg.get("linkedin_risk_ack") == "1"
    for job in rows:
        score = job["match_score"]
        score_label = f"{int(score)}/100" if score is not None else "—"
        desc = re.sub(r"<[^>]+>", " ", job["description"] or "")[:1200]
        letter = job["cover_letter"] or ""
        linkedin = is_assisted_apply_job(dict(job))
        easy_btn = ""
        if linkedin:
            if easy_on:
                easy_btn = (
                    f'<form method="post" action="/worth-easy-apply" class="js-process-form" style="display:inline">'
                    f'<input type="hidden" name="id" value="{int(job["id"])}">'
                    f'<button type="submit" class="red-accent-button" '
                    f'title="LinkedIn Easy Apply ou Apply→InHire; você confirma envio/captcha">'
                    f'Easy Apply</button></form>'
                )
            else:
                easy_btn = (
                    '<button type="button" class="subtle" disabled '
                    'title="Ative Easy Apply + aceite de risco em Perfil/IA">Easy Apply (desligado)</button>'
                )
        cards.append(
            f'<article class="worth-card">'
            f'<div class="worth-head"><a class="job-title" href="{esc(job["url"])}" target="_blank" rel="noreferrer">{esc(job["title"])}</a>'
            f'<span class="analysis-badge analysis-ok">Match {esc(score_label)}</span></div>'
            f'<p class="hint"><strong>{esc(job["company"])}</strong> · {esc(job["location"])} · {esc(job["source"])} · {esc(format_brasilia(job["posted_at"] or job["first_seen_at"]))}</p>'
            f'{_notes_html(job["notes"] or "")}'
            f'<p><a href="{esc(job["url"])}" target="_blank" rel="noreferrer">Abrir vaga para candidatura manual</a></p>'
            f'<details><summary>Descrição</summary><div class="description">{esc(desc)}</div></details>'
            f'<details><summary>{"Carta pronta para copiar" if letter else "Sem carta"}</summary><div class="description">{esc(letter or "—")}</div></details>'
            f'<div class="actions">'
            f"{easy_btn}"
            f'<form method="post" action="/job-status" class="js-process-form" style="display:inline">'
            f'<input type="hidden" name="id" value="{int(job["id"])}">'
            f'<button class="subtle" name="status" value="applied" type="submit">Marcar como aplicada</button>'
            f'<button class="subtle" name="status" value="ignored" type="submit">Ignorar</button>'
            f'<button class="subtle" name="status" value="saved" type="submit">Salvar</button>'
            f"</form></div></article>"
        )
    start = offset + 1
    end = offset + len(rows)
    filter_note = f" · match ≥ {min_match}" if min_match > 0 else ""
    pager_bits = [
        f'<div class="worth-pager" data-worth-pages="{pages}" data-worth-current="{page}">'
        f'<span class="hint">Mostrando {start}–{end} de {total}{filter_note}</span>'
        '<div class="actions" style="margin-top:0">'
    ]
    if page > 1:
        pager_bits.append(
            f'<button type="button" class="subtle" data-worth-page="{page - 1}">Anterior</button>'
        )
    else:
        pager_bits.append('<button type="button" class="subtle" disabled>Anterior</button>')
    pager_bits.append(f'<span class="hint" style="margin:0">Página {page} / {pages}</span>')
    if page < pages:
        pager_bits.append(
            f'<button type="button" class="subtle" data-worth-page="{page + 1}">Próxima</button>'
        )
    else:
        pager_bits.append('<button type="button" class="subtle" disabled>Próxima</button>')
    pager_bits.append("</div></div>")
    pager = "".join(pager_bits)
    return filter_bar + pager + '<div class="worth-list">' + "".join(cards) + "</div>" + pager


def state_label_for(state: str) -> str:
    return {"running": "Em execução", "stopping": "Parando", "stopped": "Parada"}.get(state, state)


def next_run_countdown_seconds(next_run_at: str | None) -> int | None:
    if not next_run_at:
        return None
    target = parse_utc(next_run_at)
    if not target:
        return None
    return max(0, int((target - datetime.now(timezone.utc)).total_seconds()))


def format_countdown(seconds: int | None) -> str:
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def live_payload(*, worth_page: int = 1, worth_min_match: int = 0) -> dict:
    status = collector.snapshot()
    counts, jobs, runs = load_dashboard()
    collecting = status["state"] in {"running", "stopping"}
    stats = stats_html(counts)
    jobs_body = job_rows_html(jobs, collecting)
    history = history_html(runs)
    logs = logs_html()
    queue = queue_html()
    worth_min_match = max(0, min(100, int(worth_min_match or 0)))
    worth_page = max(1, int(worth_page or 1))
    # Clamp page so "ignorar" na última página não pede um OFFSET vazio.
    worth = worth_html(page=worth_page, min_match=worth_min_match)
    # worth_html já clampou page no HTML (data-worth-current); espelha no payload.
    try:
        m = re.search(r'data-worth-current="(\d+)"', worth or "")
        if m:
            worth_page = max(1, int(m.group(1)))
    except Exception:
        pass
    linkedin_filter = linkedin_filter_panel_html()
    next_in = next_run_countdown_seconds(status.get("next_run_at"))
    return {
        "state": status["state"],
        "state_label": state_label_for(status["state"]),
        "message": status["message"],
        "next_run_at": status.get("next_run_at"),
        "next_run_in_seconds": next_in,
        "next_run_label": format_countdown(next_in) if next_in is not None else "",
        "stats_html": stats,
        "stats_hash": _live_hash(stats),
        "jobs_html": jobs_body,
        "jobs_hash": _live_hash(jobs_body),
        "history_html": history,
        "history_hash": _live_hash(history),
        "logs_html": logs,
        "logs_hash": _live_hash(logs),
        "queue_html": queue,
        "queue_hash": _live_hash(queue),
        "worth_html": worth,
        "worth_hash": _live_hash(worth),
        "worth_page": worth_page,
        "worth_min_match": worth_min_match,
        "resume_status": resume_status_payload(),
        "linkedin_filter_html": linkedin_filter,
        "linkedin_filter_hash": _live_hash(linkedin_filter),
    }


def _live_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def resume_status_parts(lang: str) -> dict[str, str]:
    """Partes do status do currículo: meta (leve) e dossiê (só muda com a análise)."""
    with connect() as db:
        row = get_resume(db, lang)
    if not row:
        meta = (
            '<div class="analysis-badge analysis-none">Sem PDF</div>'
            '<p class="hint">Nenhum currículo enviado. Escolha um PDF e clique em enviar para a IA analisar.</p>'
        )
        dossier = ""
        return {
            "meta_html": meta,
            "dossier_html": dossier,
            "meta_hash": _live_hash(meta),
            "dossier_hash": _live_hash(dossier),
        }
    raw_status = (row["analysis_status"] if "analysis_status" in row.keys() else "") or (
        "ok" if (row["analysis_summary"] or "").strip() else "none"
    )
    badge_map = {
        "ok": ("analysis-ok", "Análise OK"),
        "reused": ("analysis-info", "Análise reutilizada"),
        "pending": ("analysis-info", "Na fila de análise"),
        "error": ("analysis-error", "Erro na análise"),
        "none": ("analysis-none", "Aguardando análise"),
    }
    badge_class, badge_label = badge_map.get(raw_status, ("analysis-none", raw_status or "Desconhecido"))
    filename = esc(row["original_filename"] or "currículo.pdf")
    when = esc(format_brasilia(row["analyzed_at"])) if row["analyzed_at"] else "—"
    provider = esc(row["provider"] or "—")
    model = esc(row["model"] or "—")
    message = esc(
        (row["analysis_message"] if "analysis_message" in row.keys() else "")
        or ("Análise concluída." if raw_status in {"ok", "reused"} else "Sem mensagem.")
    )
    error = esc((row["analysis_error"] if "analysis_error" in row.keys() else "") or "")
    reanalyze = (
        f'<form method="post" action="/reanalyze-resume" class="js-process-form" style="margin-top:10px">'
        f'<input type="hidden" name="language" value="{lang}">'
        f'<button type="submit" style="width:100%">Reanalisar currículo com IA</button></form>'
        f'<p class="hint">Gera uma nova análise mesmo com o mesmo PDF (útil após melhorar o prompt).</p>'
    )
    error_block = (
        f'<p class="analysis-error-text"><strong>Detalhe do erro:</strong> {error}</p>'
        if error and raw_status == "error"
        else ""
    )
    meta = (
        f'<div class="analysis-badge {badge_class}">{badge_label}</div>'
        f'<p class="hint"><strong>Arquivo:</strong> {filename}<br>'
        f'<strong>Última análise:</strong> {when}<br>'
        f'<strong>Modelo:</strong> {provider} / {model}</p>'
        f'<p class="hint">{message}</p>'
        f"{reanalyze}{error_block}"
    )
    summary = esc((row["analysis_summary"] or "")[:6000])
    structured = ""
    try:
        data = json.loads(row["analysis_json"] or "{}")
    except json.JSONDecodeError:
        data = {}
    if isinstance(data, dict) and data:
        headline = esc(str(data.get("headline") or "")[:300])
        skills = data.get("technical_skills") or data.get("skills") or []
        tools = data.get("tools") or []
        experience = data.get("experience") or []
        if headline:
            structured += f'<p><strong>Headline:</strong> {headline}</p>'
        if isinstance(skills, list) and skills:
            structured += "<p><strong>Skills:</strong> " + esc(", ".join(str(s) for s in skills[:40])) + "</p>"
        if isinstance(tools, list) and tools:
            structured += "<p><strong>Ferramentas:</strong> " + esc(", ".join(str(s) for s in tools[:30])) + "</p>"
        if isinstance(experience, list) and experience:
            structured += "<p><strong>Experiências capturadas:</strong> " + esc(str(len(experience))) + "</p><ul>" + "".join(
                f"<li>{esc(str(item)[:280])}</li>" for item in experience[:12]
            ) + "</ul>"
    if summary:
        dossier = (
            f"{structured}"
            f'<details class="resume-dossier" open>'
            f"<summary>Dossiê completo da análise</summary>"
            f'<div class="description resume-dossier-scroll" style="max-width:100%;max-height:420px;white-space:pre-wrap">{summary}</div>'
            f"</details>"
        )
    else:
        dossier = '<p class="hint">Ainda não há dossiê salvo (análise incompleta ou falhou).</p>'
    # Hash do dossiê ignora mensagens transitórias da fila — só conteúdo da análise.
    dossier_key = "|".join(
        (
            raw_status,
            row["analyzed_at"] or "",
            row["analysis_json"] or "",
            row["analysis_summary"] or "",
            row["analysis_error"] or "",
        )
    )
    return {
        "meta_html": meta,
        "dossier_html": dossier,
        "meta_hash": _live_hash(meta),
        "dossier_hash": _live_hash(dossier_key),
    }


def resume_status_html_for(lang: str) -> str:
    """HTML combinado da zona de status (meta + dossiê). Sem input de arquivo."""
    parts = resume_status_parts(lang)
    return (
        f'<div id="resume-meta-{lang}" data-hash="{parts["meta_hash"]}">{parts["meta_html"]}</div>'
        f'<div id="resume-dossier-{lang}" data-hash="{parts["dossier_hash"]}">{parts["dossier_html"]}</div>'
    )


def resume_status_payload() -> dict[str, dict[str, str]]:
    return {"pt": resume_status_parts("pt"), "en": resume_status_parts("en")}


def resume_panels_html() -> str:
    blocks = []
    for lang, label in (("pt", "Português"), ("en", "English")):
        blocks.append(
            f'<div class="resume-card"><h3 style="margin:0 0 8px;font-size:15px">Currículo {label}</h3>'
            f'<form method="post" action="/upload-resume" enctype="multipart/form-data" class="js-process-form">'
            f'<input type="hidden" name="language" value="{lang}">'
            f'<label>Selecionar PDF<input type="file" name="file" accept="application/pdf" required></label>'
            f'<label style="margin-top:8px;font-weight:500">'
            f'<input type="checkbox" name="force_reanalyze" value="1" style="width:auto;margin-right:6px">'
            f'Forçar nova análise mesmo se o arquivo for idêntico</label>'
            f'<button style="margin-top:8px">Enviar e analisar</button></form>'
            f'<div id="resume-status-{lang}">{resume_status_html_for(lang)}</div></div>'
        )
    return '<div class="form-grid">' + "".join(blocks) + "</div>"


def request_wants_json(headers) -> bool:
    accept = (headers.get("Accept") or "").casefold()
    if "application/json" in accept:
        return True
    return (headers.get("X-Requested-With") or "").casefold() == "fetch"


def form_rules_html() -> str:
    with connect() as db:
        rules = list_rules(db)
    rows = []
    for idx, rule in enumerate(rules, start=1):
        mode_opts = "".join(
            f'<option value="{m}" {"selected" if rule["mode"] == m else ""}>{m}</option>'
            for m in ("text", "select", "salary", "file", "cover_letter", "skip")
        )
        rows.append(
            f'<tr><td><input name="rule_key_{idx}" value="{esc(rule["key"])}"></td>'
            f'<td><input name="rule_aliases_{idx}" value="{esc(rule["aliases"])}"></td>'
            f'<td><select name="rule_mode_{idx}">{mode_opts}</select></td>'
            f'<td><input name="rule_value_from_{idx}" value="{esc(rule["value_from"])}" placeholder="ex.: candidate_phone"></td>'
            f'<td><input name="rule_value_{idx}" value="{esc(rule["value"])}"></td></tr>'
        )
    idx = len(rules) + 1
    mode_opts = "".join(f'<option value="{m}">{m}</option>' for m in ("text", "select", "salary", "file", "cover_letter", "skip"))
    rows.append(
        f'<tr><td><input name="rule_key_{idx}" placeholder="nova chave"></td>'
        f'<td><input name="rule_aliases_{idx}" placeholder="aliases"></td>'
        f'<td><select name="rule_mode_{idx}">{mode_opts}</select></td>'
        f'<td><input name="rule_value_from_{idx}"></td>'
        f'<td><input name="rule_value_{idx}"></td></tr>'
    )
    return (
        '<table style="min-width:100%;font-size:13px"><thead><tr>'
        "<th>Chave</th><th>Aliases</th><th>Modo</th><th>Valor de settings</th><th>Valor fixo / select</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


PAGE_CSS = """*{box-sizing:border-box}
:root{--bg:#0a0a0b;--panel:#121214;--panel2:#17171a;--line:#26262b;--line2:#33333a;
--ink:#ededf0;--muted:#8a8a93;--muted2:#5c5c66;--accent:#e5484d;--accent2:#ff6369;
--ok:#4ea36b;--info:#3f6f9e;--warn:#b98a3a;--err:#c2544d;--white:#fff}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);
font:14.5px/1.55 ui-sans-serif,Inter,Segoe UI,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.shell{display:grid;grid-template-columns:248px 1fr;min-height:100vh}
.sidebar{background:#0c0c0e;border-right:1px solid var(--line);display:flex;flex-direction:column;
position:sticky;top:0;height:100vh;padding:20px 14px;gap:18px}
.brand strong{display:block;font-size:15px;letter-spacing:.02em;color:var(--white)}
.brand small{display:block;color:var(--muted2);font-size:11.5px;margin-top:4px;font-weight:500}
.brand{border-bottom:1px solid var(--line);padding-bottom:14px}
.nav-label{display:block;color:var(--muted2);font-size:10.5px;font-weight:700;letter-spacing:.14em;
text-transform:uppercase;margin:6px 8px 4px}
.tabs{display:flex;flex-direction:column;gap:2px}
.tab{background:transparent;border:0;color:var(--muted);text-align:left;padding:9px 11px;border-radius:8px;
font:inherit;font-weight:600;font-size:13.5px;cursor:pointer;display:flex;align-items:center;gap:9px;
border-left:2px solid transparent;transition:background .12s,color .12s}
.tab:hover{background:var(--panel2);color:var(--ink)}
.tab.active{background:var(--panel2);color:var(--white);border-left:2px solid var(--accent)}
.sidebar-foot{margin-top:auto;border-top:1px solid var(--line);padding-top:14px}
#collector-state{font-size:12px;font-weight:700;color:var(--muted);display:inline-flex;align-items:center;gap:7px}
#collector-state::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--muted2)}
.content{padding:26px max(24px,calc((100vw - 1600px)/2));overflow-x:auto}
h1{font-size:20px;margin:0}
h2{font-size:16px;margin:0 0 16px;font-weight:700;letter-spacing:-.01em;color:var(--white)}
h3{font-size:13px}
main{max-width:1180px;margin:0 auto;padding:0 24px}
.top{display:grid;grid-template-columns:1.3fr .7fr;gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px}
.runbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:14px 18px}
.runbar .actions{margin-top:0}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
label{display:block;color:var(--muted);font-size:12.5px;font-weight:600}
input,textarea,select{font:inherit;color:var(--ink);width:100%;margin-top:6px;padding:9px 11px;
border:1px solid var(--line2);border-radius:9px;background:#0e0e10;transition:border-color .12s,background .12s}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent);background:#101012}
input[type=checkbox]{width:auto;margin:0;accent-color:var(--accent)}
option{background:#0e0e10;color:var(--ink)}
.hint{color:var(--muted2);font-size:11.5px;margin:12px 0 0;line-height:1.5}
.hint code{background:#000;border:1px solid var(--line);border-radius:5px;padding:1px 5px;color:var(--muted);font-size:11px}
button{border:1px solid transparent;border-radius:9px;padding:9px 15px;background:var(--white);color:#0a0a0a;
font-weight:700;font-size:13px;cursor:pointer;transition:filter .12s,background .12s}
button:hover{filter:brightness(.92)}
button.stop{background:var(--accent);color:var(--white)}
button.subtle{padding:6px 10px;background:transparent;color:var(--muted);border:1px solid var(--line2);font-weight:600}
button.subtle:hover{color:var(--ink);border-color:var(--muted2);background:var(--panel2)}
.actions{display:flex;gap:9px;margin-top:14px;align-items:center;flex-wrap:wrap}
.runtime{color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:4px 0 18px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 15px}
.stat span{display:block;font-size:11px;color:var(--muted2)}
.stat strong{font-size:22px;color:var(--white);font-variant-numeric:tabular-nums}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;min-width:900px;font-size:13px}
th,td{padding:11px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:10.5px;color:var(--muted2);background:#0f0f11;text-transform:uppercase;letter-spacing:.08em}
tr:last-child td{border-bottom:0}
td small{display:block;color:var(--muted2);margin-top:3px}
.job-title{font-weight:700;color:var(--white);text-decoration:none}
.job-title:hover{color:var(--accent2)}
.source{background:var(--panel2);border:1px solid var(--line);color:var(--muted);padding:2px 10px;
border-radius:99px;font-size:11px;display:inline-block;max-width:140px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap;vertical-align:middle;line-height:1.6}
select{min-width:150px;margin:0;padding:7px}
summary{cursor:pointer;color:var(--muted);font-size:13px}
.description{max-width:360px;max-height:220px;overflow:auto;padding:8px 0;font-size:12.5px;color:var(--muted)}
details textarea{min-width:230px}
.history{color:var(--muted);font-size:12.5px;padding-left:18px}
.notice{padding:11px 14px;border-radius:10px;margin-bottom:16px;font-size:13px;font-weight:600;
border:1px solid var(--line2);background:var(--panel2)}
.notice-ok{border-color:var(--ok);color:#8fd6a6;background:rgba(78,163,107,.1)}
.notice-info{border-color:var(--info);color:#9ec9ff;background:rgba(63,111,158,.12)}
.notice-error{border-color:var(--err);color:#f0a0a0;background:rgba(194,84,77,.12)}
.notice-warn{border-color:var(--warn);color:#f0c674;background:rgba(185,138,58,.1)}
.analysis-badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:11.5px;font-weight:700;margin:6px 0}
.analysis-ok{background:rgba(78,163,107,.16);color:#8fd6a6}
.analysis-info{background:rgba(63,111,158,.2);color:#9ec9ff}
.analysis-error{background:rgba(194,84,77,.2);color:#f0a0a0}
.analysis-none{background:var(--panel2);color:var(--muted)}
.analysis-error-text{color:#f0a0a0;font-size:12.5px;margin:8px 0}
.red-accent-button{background:var(--accent);color:var(--white);border:1px solid var(--accent);border-radius:9px;padding:9px 15px;font-weight:700;font-size:13px;cursor:pointer;transition:filter .12s,background .12s}
.red-accent-button:hover{filter:brightness(.92);background:var(--accent2);border-color:var(--accent2)}
.resume-card{border:1px solid var(--line);border-radius:12px;padding:16px;background:var(--panel2)}
.resume-dossier summary{color:var(--muted)}
.resume-dossier-scroll{background:#0e0e10}
.log-console{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#08080a;
color:#c9c9d1;border:1px solid var(--line);border-radius:10px;padding:12px;max-height:600px;overflow:auto}
.log-line{display:grid;grid-template-columns:128px 66px 104px 1fr;gap:10px;padding:5px 0;border-bottom:1px solid #141417}
.log-time{color:var(--muted2)}
.log-level{font-weight:700;text-transform:uppercase}
.log-source{color:var(--muted)}
.log-msg{color:#d6d6de;white-space:pre-wrap;word-break:break-word}
.log-info .log-level{color:#9ec9ff}
.log-success .log-level{color:#8fd6a6}
.log-warning .log-level{color:#f0c674}
.log-error .log-level{color:#f0a0a0}
.log-debug .log-level{color:var(--muted2)}
.log-empty{color:var(--muted2);padding:18px 8px}
.queue-status{display:inline-block;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:700}
.queue-pending,.queue-retry_wait{background:rgba(63,111,158,.2);color:#9ec9ff}
.queue-running{background:rgba(78,163,107,.16);color:#8fd6a6}
.queue-succeeded{background:rgba(78,163,107,.16);color:#8fd6a6}
.queue-failed{background:rgba(194,84,77,.2);color:#f0a0a0}
.queue-cancelled{background:var(--panel2);color:var(--muted)}
.worth-list{display:grid;gap:14px}
.worth-card{border:1px solid var(--line);border-radius:12px;padding:15px;background:var(--panel)}
.worth-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
.worth-pager{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin:8px 0 14px}
.tab-panel{display:none}
.tab-panel.active{display:block}
.subtle{color:var(--muted)}
@media(max-width:900px){.log-line{grid-template-columns:1fr;gap:2px}}
@media(max-width:820px){.shell{grid-template-columns:1fr}.sidebar{position:static;height:auto}
.top{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}
.form-grid{grid-template-columns:1fr}}"""

def render_page(notice: str = "", notice_kind: str = "success") -> str:
    """Monta o painel local: preferências, controles, histórico e vagas capturadas."""
    cfg = settings()
    status = collector.snapshot()
    counts, jobs, runs = load_dashboard()
    collecting = status["state"] in {"running", "stopping"}
    cards = stats_html(counts)
    rows = job_rows_html(jobs, collecting)
    history = history_html(runs)
    state_label = state_label_for(status["state"])
    next_in = next_run_countdown_seconds(status.get("next_run_at"))
    if status["state"] == "stopped":
        next_run_timer_text = "Próxima busca: — (bot parado)"
    elif next_in is not None:
        next_run_timer_text = f"Próxima busca em {format_countdown(next_in)}"
    else:
        next_run_timer_text = "Próxima busca: em andamento…"
    resume_panel = resume_panels_html()
    rules_panel = form_rules_html()
    logs_view = logs_html()
    queue_view = queue_html()
    worth_view = worth_html()
    linkedin_filter_view = linkedin_filter_panel_html(cfg)
    notice_class = {
        "success": "notice notice-ok",
        "info": "notice notice-info",
        "error": "notice notice-error",
        "warning": "notice notice-warn",
    }.get(notice_kind, "notice") if notice else "notice"
    notice_html = (
        f'<div id="panel-notice" class="{notice_class}"'
        f'{" hidden" if not notice else ""}>'
        f"{esc(notice) if notice else ''}</div>"
    )
    return f'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar de Vagas</title>
<style>{PAGE_CSS}</style></head><body>
<div class="shell">
<aside class="sidebar">
<div class="brand"><strong>Radar de Vagas</strong><small>coleta &middot; triagem &middot; candidaturas</small></div>
<nav class="tabs" aria-label="Seções do painel">
<span class="nav-label">Operação</span>
<button type="button" class="tab active" data-tab="painel">Visão geral</button>
<button type="button" class="tab" data-tab="vale">Vale a pena olhar</button>
<button type="button" class="tab" data-tab="filas">Filas de IA</button>
<button type="button" class="tab" data-tab="logs">Logs</button>
<span class="nav-label">Configuração</span>
<button type="button" class="tab" data-tab="busca">Busca &amp; coleta</button>
<button type="button" class="tab" data-tab="curriculos">Currículos</button>
<button type="button" class="tab" data-tab="perfil">Perfil &amp; diversidade</button>
<button type="button" class="tab" data-tab="ia">IA &amp; integrações</button>
<button type="button" class="tab" data-tab="automacao">Automação &amp; LinkedIn</button>
<button type="button" class="tab" data-tab="smtp">SMTP</button>
</nav>
<div class="sidebar-foot"><span id="collector-state">{esc(state_label)}</span></div>
</aside>
<main class="content">{notice_html}

<div id="tab-painel" class="tab-panel active">
<section class="panel runbar"><div class="actions" style="margin:0"><form method="post" action="/start" class="js-process-form"><button>Iniciar bot</button></form><form method="post" action="/stop" class="js-process-form"><button class="stop">Parar bot</button></form><span class="runtime" id="runtime-message">{esc(status['message'])}</span><span class="runtime" id="next-run-timer" style="margin-left:14px">{esc(next_run_timer_text)}</span></div></section>
<div class="stats" id="job-stats">{cards}</div>
<section class="table-wrap"><table><thead><tr><th>Vaga</th><th>Localidade</th><th>Fonte</th><th>Idioma</th><th>Etapa</th><th>Carta e descrição</th><th>Data</th></tr></thead><tbody id="jobs-body">{rows}</tbody></table></section>
<section class="panel" style="margin-top:18px"><h2>Execuções recentes</h2><ul class="history" id="run-history">{history}</ul></section>
</div>

<div id="tab-busca" class="tab-panel"><section class="panel"><h2>Busca &amp; coleta</h2><form method="post" action="/settings"><div class="form-grid"><label>Cargos e termos, separados por vírgula<textarea name="keywords" rows="3">{esc(cfg.get('keywords',''))}</textarea></label><label>Países/regiões aceitos<textarea name="locations" rows="3">{esc(cfg.get('locations',''))}</textarea></label><label>Fontes: remotive, remoteok, adzuna, apify<input name="sources" value="{esc(cfg.get('sources',''))}"></label><label>Intervalo de busca (minutos)<input name="interval_minutes" type="number" min="5" value="{esc(cfg.get('interval_minutes','15'))}"></label><label>Países Adzuna (ex.: br,us,gb,ca)<input name="adzuna_countries" value="{esc(cfg.get('adzuna_countries','br,us,gb,ca'))}"></label><label>Limite mensal Apify (USD)<input name="apify_monthly_credit_limit_usd" type="number" min="0" step="0.01" value="{esc(cfg.get('apify_monthly_credit_limit_usd','5'))}"></label><label>Máximo de vagas por ciclo Apify<input name="apify_job_count" type="number" min="1" max="100" value="{esc(cfg.get('apify_job_count','25'))}"></label><label style="grid-column:1/-1">Actors Apify (JSON — um ou mais scrapers)<textarea name="apify_actors_json" rows="8" style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px">{esc(cfg.get('apify_actors_json',''))}</textarea></label></div><p class="hint">Cada actor: <code>id</code>, <code>label</code>, <code>enabled</code>, <code>input_mode</code> (<code>linkedin_search</code> ou <code>custom</code>), <code>count</code> opcional. Em <code>linkedin_search</code>, a IA monta sozinha keywords + f_E (nível) + f_WT (remote/híbrido) a partir do currículo (cache até reanalisar). Em <code>custom</code>, use <code>input_template</code> com placeholders <code>{{keyword}}</code>, <code>{{location}}</code>, <code>{{count}}</code>, <code>{{keywords}}</code>.</p><div id="linkedin-filter-slot">{linkedin_filter_view}</div><button>Salvar preferências</button></form></section></div>

<div id="tab-curriculos" class="tab-panel"><section class="panel"><h2>Currículos (PDF)</h2>{resume_panel}<p class="hint">O seletor de arquivos do sistema abre ao escolher o PDF. A análise agora gera um dossiê completo (skills, experiências, projetos). Use <em>Reanalisar</em> para regenerar com o prompt enriquecido.</p></section></div>

<div id="tab-perfil" class="tab-panel"><section class="panel"><h2>Perfil &amp; diversidade</h2><form method="post" action="/candidate-settings"><h3 style="margin:0 0 12px;color:var(--muted)">Dados pessoais</h3><div class="form-grid"><label>Seu nome<input name="candidate_name" value="{esc(cfg.get('candidate_name',''))}"></label><label>E-mail<input name="candidate_email" value="{esc(cfg.get('candidate_email',''))}"></label><label>Telefone<input name="candidate_phone" value="{esc(cfg.get('candidate_phone',''))}"></label><label>LinkedIn<input name="candidate_linkedin" value="{esc(cfg.get('candidate_linkedin',''))}"></label><label>Cidade<input name="candidate_city" value="{esc(cfg.get('candidate_city',''))}"></label><label>CPF<input name="candidate_cpf" value="{esc(cfg.get('candidate_cpf',''))}" placeholder="000.000.000-00"></label></div><h3 style="margin:20px 0 12px;color:var(--muted)">Pretensão salarial &amp; contratação</h3><div class="form-grid"><label>Pretensão base — CLT (BRL)<input name="salary_expectation_brl" value="{esc(cfg.get('salary_expectation_brl',''))}" placeholder="ex.: 6000"></label><label>Pretensão (USD)<input name="salary_expectation_usd" value="{esc(cfg.get('salary_expectation_usd',''))}" placeholder="ex.: 80000"></label><label>Moeda da pretensão<select name="salary_currency_preference"><option value="auto" {'selected' if cfg.get('salary_currency_preference','auto')=='auto' else ''}>Detectar na página (padrão)</option><option value="BRL" {'selected' if cfg.get('salary_currency_preference')=='BRL' else ''}>Sempre BRL (R$)</option><option value="USD" {'selected' if cfg.get('salary_currency_preference')=='USD' else ''}>Sempre USD ($)</option></select></label><label>Multiplicador PJ (só BRL)<input name="salary_pj_multiplier" value="{esc(cfg.get('salary_pj_multiplier','1.3'))}" placeholder="ex.: 1.3" step="0.05" min="1"></label><label>Tipo de contratação preferido<select name="candidate_contract_type"><option value="employee" {'selected' if cfg.get('candidate_contract_type','employee')=='employee' else ''}>Employee (CLT / efetivo)</option><option value="contractor" {'selected' if cfg.get('candidate_contract_type')=='contractor' else ''}>Contractor (PJ / autônomo)</option><option value="ask" {'selected' if cfg.get('candidate_contract_type')=='ask' else ''}>Perguntar — deixar comigo no Chrome</option></select></label></div><p class="hint">A pretensão usa o valor BRL ou USD conforme a moeda do campo na vaga (placeholder <code>R$</code> vs <code>$</code>). O valor <strong>BRL é a base CLT</strong>: se a contratação preferida for <strong>PJ/Contractor</strong>, ele é multiplicado pelo <strong>multiplicador PJ</strong> (aceita decimal; só em reais — USD nunca é multiplicado). O radio Contractor/Employee segue a preferência (com "Perguntar" o robô deixa você decidir).</p><h3 style="margin:22px 0 12px;color:var(--muted)">Perfil de diversidade (a IA nunca decide identidade)</h3><div class="form-grid"><label>Gênero<select name="candidate_gender"><option value="not_informed" {'selected' if cfg.get('candidate_gender','not_informed') == 'not_informed' else ''}>Prefiro não informar</option><option value="female" {'selected' if cfg.get('candidate_gender') == 'female' else ''}>Mulher</option><option value="male" {'selected' if cfg.get('candidate_gender') == 'male' else ''}>Homem</option><option value="other" {'selected' if cfg.get('candidate_gender') == 'other' else ''}>Outro / não binário</option></select></label><label>Raça/etnia (autodeclarada)<select name="candidate_race"><option value="not_informed" {'selected' if cfg.get('candidate_race','not_informed') == 'not_informed' else ''}>Prefiro não informar</option><option value="white" {'selected' if cfg.get('candidate_race') == 'white' else ''}>Branca</option><option value="black" {'selected' if cfg.get('candidate_race') == 'black' else ''}>Preta</option><option value="pardo" {'selected' if cfg.get('candidate_race') == 'pardo' else ''}>Parda</option><option value="asian" {'selected' if cfg.get('candidate_race') == 'asian' else ''}>Amarela/asiática</option><option value="indigenous" {'selected' if cfg.get('candidate_race') == 'indigenous' else ''}>Indígena</option></select></label><label>PCD (deficiência)<select name="candidate_pcd"><option value="no" {'selected' if cfg.get('candidate_pcd','no') in ('no','0') else ''}>Não sou PCD</option><option value="yes" {'selected' if cfg.get('candidate_pcd') in ('yes','1') else ''}>Sou PCD</option><option value="not_informed" {'selected' if cfg.get('candidate_pcd') == 'not_informed' else ''}>Prefiro não informar</option></select></label><label>LGBTQ+ (orientação/identidade)<select name="candidate_lgbtq"><option value="not_informed" {'selected' if cfg.get('candidate_lgbtq','not_informed') == 'not_informed' else ''}>Prefiro não informar</option><option value="no" {'selected' if cfg.get('candidate_lgbtq') == 'no' else ''}>Hétero / cis (não)</option><option value="gay" {'selected' if cfg.get('candidate_lgbtq') == 'gay' else ''}>Homossexual / gay</option><option value="lesbian" {'selected' if cfg.get('candidate_lgbtq') == 'lesbian' else ''}>Lésbica</option><option value="bisexual" {'selected' if cfg.get('candidate_lgbtq') == 'bisexual' else ''}>Bissexual</option><option value="pansexual" {'selected' if cfg.get('candidate_lgbtq') == 'pansexual' else ''}>Pansexual</option><option value="asexual" {'selected' if cfg.get('candidate_lgbtq') == 'asexual' else ''}>Assexual</option><option value="other" {'selected' if cfg.get('candidate_lgbtq') == 'other' else ''}>Outra (use a nota)</option><option value="yes" {'selected' if cfg.get('candidate_lgbtq') == 'yes' else ''}>Sou LGBTI+ (genérico)</option></select></label></div><label style="margin-top:14px">Nota livre p/ perguntas de diversidade fora das opções<textarea name="candidate_diversity_note" rows="2">{esc(cfg.get('candidate_diversity_note',''))}</textarea></label><p class="hint">Esses valores respondem perguntas de diversidade (radio/select/checkbox). "Prefiro não informar" deixa a pergunta em branco para você no Chrome — o robô não chuta. Perguntas da empresa <em>sem</em> relação com identidade são respondidas pela IA com a análise do currículo.</p><button style="margin-top:16px">Salvar perfil</button></form></section></div>

<div id="tab-ia" class="tab-panel"><section class="panel"><h2>IA &amp; integrações</h2><form method="post" action="/ai-settings"><div class="form-grid"><label>Provedor de IA<select name="ai_provider"><option value="gemini" {'selected' if cfg.get('ai_provider') == 'gemini' else ''}>Gemini</option><option value="openai" {'selected' if cfg.get('ai_provider') == 'openai' else ''}>OpenAI</option></select></label><label>Modelo<input name="ai_model" value="{esc(cfg.get('ai_model','gemini-2.5-flash'))}"></label><label>Motor de navegador<select name="browser_engine"><option value="pydoll" {'selected' if cfg.get('browser_engine','pydoll') == 'pydoll' else ''}>Pydoll (CDP, stealth — padrão)</option><option value="playwright" {'selected' if cfg.get('browser_engine','pydoll') == 'playwright' else ''}>Playwright (fallback)</option></select></label><label>Chave de IA (vazio mantém a salva)<input type="password" name="api_key" autocomplete="new-password"></label><label>Adzuna App ID<input name="adzuna_app_id" value=""></label><label>Adzuna API key<input type="password" name="adzuna_app_key" value=""></label><label>Token Apify<input type="password" name="apify_token" value="" autocomplete="new-password"></label></div><label style="margin-top:14px">Fatos profissionais em português<textarea name="candidate_facts_pt" rows="3">{esc(cfg.get('candidate_facts_pt',''))}</textarea></label><label style="margin-top:12px">Professional facts in English<textarea name="candidate_facts_en" rows="3">{esc(cfg.get('candidate_facts_en',''))}</textarea></label><p class="hint">Chaves vão para o cofre do sistema. Os fatos alimentam a IA nas perguntas abertas e de opções.</p><button style="margin-top:14px">Salvar IA e integrações</button></form></section><section class="panel"><h2>Regras de formulário (navegador)</h2><form method="post" action="/profile-settings">{rules_panel}<p class="hint">Modo <code>salary</code> escolhe automaticamente BRL×USD pela moeda do campo e aplica o multiplicador PJ ao valor BRL quando a contratação preferida é PJ; "Valor fixo" só é usado como desempate. No modo <code>select</code>, coloque em "Valor fixo" o texto da opção preferida. Perguntas abertas sem regra usam a IA.</p><button>Salvar regras</button></form></section></div>

<div id="tab-automacao" class="tab-panel"><section class="panel"><h2>Automação &amp; LinkedIn</h2><form method="post" action="/automation-settings"><label style="margin:0 0 12px;display:flex;gap:8px;align-items:flex-start"><input type="checkbox" name="auto_apply" value="1" {'checked' if cfg.get('auto_apply') == '1' else ''} style="width:auto;margin-top:3px"> <span>Ativar triagem e candidatura automáticas (e-mail SMTP, depois formulário público)</span></label><label style="margin:0 0 12px;display:flex;gap:8px;align-items:flex-start"><input type="checkbox" name="linkedin_easy_apply" value="1" {'checked' if cfg.get('linkedin_easy_apply') == '1' else ''} style="width:auto;margin-top:3px"> <span>Easy Apply LinkedIn <em>assistido</em> (preenche; <strong>você</strong> clica Enviar — o robô nunca envia sozinho)</span></label><label style="margin:0 0 18px;display:flex;gap:8px;align-items:flex-start"><input type="checkbox" name="linkedin_risk_ack" value="1" {'checked' if cfg.get('linkedin_risk_ack') == '1' else ''} style="width:auto;margin-top:3px"> <span>Li e aceito: automação no LinkedIn pode violar os termos deles e gerar restrição/banimento; uso por minha conta e risco</span></label><div class="form-grid"><label>Score mínimo (%)<input name="minimum_match_score" type="number" min="0" max="100" value="{esc(cfg.get('minimum_match_score','80'))}"></label><label>Workers da fila (vagas em paralelo)<input name="queue_max_workers" type="number" min="1" max="8" value="{esc(cfg.get('queue_max_workers','3'))}"></label><label>Máx. tentativas por job<input name="queue_max_attempts" type="number" min="1" max="200" value="{esc(cfg.get('queue_max_attempts','40'))}"></label><label>TTL da fila (horas)<input name="queue_ttl_hours" type="number" min="1" max="168" value="{esc(cfg.get('queue_ttl_hours','24'))}"></label><label>Intervalo mínimo entre Easy Apply (min, 1–30)<input name="linkedin_min_gap_minutes" type="number" min="1" max="30" value="{esc(cfg.get('linkedin_min_gap_minutes','3'))}"></label><label>Tempo para você revisar/enviar (min)<input name="linkedin_human_wait_minutes" type="number" min="3" max="45" value="{esc(cfg.get('linkedin_human_wait_minutes','12'))}"></label><label>Tempo para login manual (min)<input name="linkedin_login_wait_minutes" type="number" min="5" max="60" value="{esc(cfg.get('linkedin_login_wait_minutes','25'))}"></label><label>Perfil Chrome dedicado<input name="linkedin_chrome_profile" value="{esc(cfg.get('linkedin_chrome_profile') or '')}" placeholder="{esc(default_profile_dir(ROOT))}"></label></div><p class="hint">LinkedIn assistido: Easy Apply <em>ou</em> Apply externo (ex.: InHire) — no máximo 1 Easy Apply por vez na fila, só intervalo mínimo entre ações (sem limite diário), checkpoint/captcha com você. Sem o aceite de risco, Easy Apply não roda. PCD e demais dados de diversidade ficam em Perfil.</p><button style="margin-top:14px">Salvar automação</button></form></section></div>

<div id="tab-smtp" class="tab-panel"><section class="panel"><h2>SMTP</h2><form method="post" action="/smtp-settings"><div class="form-grid"><label>Host<input name="smtp_host" value="{esc(cfg.get('smtp_host',''))}"></label><label>Porta<input name="smtp_port" type="number" value="{esc(cfg.get('smtp_port','587'))}"></label><label>Usuário<input name="smtp_user" value="{esc(cfg.get('smtp_user',''))}"></label><label>Remetente (From)<input name="smtp_from" value="{esc(cfg.get('smtp_from',''))}"></label><label>Senha (vazio mantém)<input type="password" name="smtp_password" autocomplete="new-password"></label><label>TLS<select name="smtp_use_tls"><option value="1" {'selected' if cfg.get('smtp_use_tls','1')=='1' else ''}>Sim (STARTTLS)</option><option value="0" {'selected' if cfg.get('smtp_use_tls')=='0' else ''}>Não</option></select></label></div><div class="actions"><button>Salvar SMTP</button></div></form><form method="post" action="/smtp-test" style="margin-top:8px"><button class="subtle" type="submit">Enviar e-mail de teste</button></form></section></div>

<div id="tab-vale" class="tab-panel"><section class="panel"><h2>Vale a pena olhar</h2><p class="hint">Vagas com bom match em LinkedIn (Easy Apply desligado/falhou) ou em que e-mail/formulário automático não funcionou. Em vagas LinkedIn, use <strong>Easy Apply</strong> para preencher sem nova busca — você confirma o envio no Chrome.</p><div id="worth-body">{worth_view}</div></section></div>

<div id="tab-filas" class="tab-panel"><section class="panel"><h2>Filas de IA (async + retry)</h2><p class="hint">Análise de currículo e candidaturas rodam em paralelo (até 3 workers). Em fila/rate-limit da API, o job entra em retry automático até sucesso, expirar (24h) ou cancelar.</p><div id="queue-body">{queue_view}</div></section></div>

<div id="tab-logs" class="tab-panel"><section class="panel"><div class="actions" style="justify-content:space-between;margin-top:0"><h2 style="margin:0">Logs do processo</h2><form method="post" action="/clear-logs" class="js-process-form"><button class="subtle" type="submit">Limpar logs</button></form></div><p class="hint">Atualiza automaticamente. Mostra coleta, análise de currículo, triagem da IA, SMTP e navegador.</p><div id="logs-body" class="log-console">{logs_view}</div></section></div>

</main></div>
<script>
(function () {{
  var inFlight = false;
  var nextRunAtIso = {json.dumps(status.get("next_run_at"))};
  var collectorState = {json.dumps(status["state"])};
  function formatCountdownClient(sec) {{
    sec = Math.max(0, Math.floor(sec));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    if (h > 0) return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
    return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }}
  function updateNextRunTimer() {{
    var el = document.getElementById("next-run-timer");
    if (!el) return;
    if (collectorState === "stopped") {{
      el.textContent = "Próxima busca: — (bot parado)";
      return;
    }}
    if (!nextRunAtIso) {{
      el.textContent = "Próxima busca: em andamento…";
      return;
    }}
    var target = Date.parse(nextRunAtIso);
    if (!target) {{
      el.textContent = "Próxima busca: —";
      return;
    }}
    var left = Math.max(0, Math.round((target - Date.now()) / 1000));
    el.textContent = "Próxima busca em " + formatCountdownClient(left);
  }}
  document.querySelectorAll(".tab").forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      var name = btn.getAttribute("data-tab");
      document.querySelectorAll(".tab").forEach(function (el) {{ el.classList.toggle("active", el === btn); }});
      document.querySelectorAll(".tab-panel").forEach(function (panel) {{
        panel.classList.toggle("active", panel.id === "tab-" + name);
      }});
      try {{ localStorage.setItem("radar-tab", name); }} catch (e) {{}}
    }});
  }});
  try {{
    var saved = localStorage.getItem("radar-tab");
    if (saved) {{
      var target = document.querySelector('.tab[data-tab="' + saved + '"]');
      if (target) target.click();
    }}
  }} catch (e) {{}}
  var appliedHashes = {{}};
  var worthPage = 1;
  var worthMinMatch = 0;
  var worthForceUpdate = false;
  try {{
    var savedPage = parseInt(localStorage.getItem("radar-worth-page"), 10);
    if (savedPage > 0) worthPage = savedPage;
    var savedMin = parseInt(localStorage.getItem("radar-worth-min-match"), 10);
    if (!isNaN(savedMin)) worthMinMatch = Math.max(0, Math.min(100, savedMin));
  }} catch (e) {{}}
  function persistWorthState() {{
    try {{
      localStorage.setItem("radar-worth-page", String(worthPage));
      localStorage.setItem("radar-worth-min-match", String(worthMinMatch));
    }} catch (e) {{}}
  }}
  function panelBusy(el) {{
    if (!el) return false;
    try {{
      if (el.matches(":hover")) return true;
      if (el.querySelector(":hover")) return true;
    }} catch (e) {{}}
    if (el.contains(document.activeElement)) return true;
    return false;
  }}
  function applyRegion(el, html, hash, opts) {{
    opts = opts || {{}};
    if (!el || html == null || hash == null) return;
    if (appliedHashes[opts.key] === hash || el.getAttribute("data-hash") === hash) {{
      appliedHashes[opts.key] = hash;
      return;
    }}
    if (opts.skipIfBusy && panelBusy(el)) return;
    var scrollEl = opts.scrollSelector ? el.querySelector(opts.scrollSelector) : null;
    var scrollTop = scrollEl ? scrollEl.scrollTop : 0;
    var details = opts.preserveDetails ? el.querySelector("details") : null;
    var wasOpen = details ? details.open : null;
    var wrap = opts.wrapScroll ? el.closest(opts.wrapScroll) : null;
    var wrapTop = wrap ? wrap.scrollTop : 0;
    el.innerHTML = html;
    el.setAttribute("data-hash", hash);
    appliedHashes[opts.key] = hash;
    if (scrollEl) {{
      var again = el.querySelector(opts.scrollSelector);
      if (again) again.scrollTop = scrollTop;
    }}
    if (details != null) {{
      var d2 = el.querySelector("details");
      if (d2 && wasOpen !== null) d2.open = wasOpen;
    }}
    if (wrap) wrap.scrollTop = wrapTop;
  }}
  function requestWorthRefresh() {{
    appliedHashes.worth = null;
    worthForceUpdate = true;
    var worthEl = document.getElementById("worth-body");
    if (worthEl) worthEl.removeAttribute("data-hash");
    refresh();
  }}
  function setWorthIgnoreConfirm(open) {{
    var btn = document.getElementById("worth-ignore-all");
    var box = document.getElementById("worth-ignore-all-confirm");
    if (!btn || !box) return;
    if (open) {{
      btn.hidden = true;
      box.hidden = false;
      box.style.display = "inline-flex";
    }} else {{
      btn.hidden = false;
      box.hidden = true;
      box.style.display = "none";
    }}
  }}
  function ignoreAllWorth() {{
    fetch("/worth-ignore-all", {{
      method: "POST",
      body: new FormData(),
      headers: {{ Accept: "application/json", "X-Requested-With": "fetch" }},
      credentials: "same-origin"
    }})
      .then(function (r) {{ return r.json().then(function (data) {{ return {{ okHttp: r.ok, data: data }}; }}); }})
      .then(function (res) {{
        var data = res.data || {{}};
        showNotice(data.notice || (data.ok ? "OK" : "Falha"), data.kind || (data.ok ? "success" : "error"));
        worthPage = 1;
        persistWorthState();
        requestWorthRefresh();
      }})
      .catch(function () {{
        showNotice("Falha de comunicação com o painel.", "error");
        setWorthIgnoreConfirm(false);
      }});
  }}
  var worthBody = document.getElementById("worth-body");
  if (worthBody) {{
    worthBody.addEventListener("click", function (ev) {{
      var preset = ev.target.closest(".worth-match-preset");
      if (preset && worthBody.contains(preset)) {{
        worthMinMatch = parseInt(preset.getAttribute("data-worth-min-match"), 10) || 0;
        worthPage = 1;
        persistWorthState();
        requestWorthRefresh();
        return;
      }}
      if (ev.target && ev.target.id === "worth-apply-filter") {{
        var input = document.getElementById("worth-min-match");
        var val = input ? parseInt(input.value, 10) : 0;
        if (isNaN(val)) val = 0;
        worthMinMatch = Math.max(0, Math.min(100, val));
        worthPage = 1;
        persistWorthState();
        requestWorthRefresh();
        return;
      }}
      if (ev.target && ev.target.id === "worth-ignore-all") {{
        setWorthIgnoreConfirm(true);
        return;
      }}
      if (ev.target && ev.target.id === "worth-ignore-all-no") {{
        setWorthIgnoreConfirm(false);
        return;
      }}
      if (ev.target && ev.target.id === "worth-ignore-all-yes") {{
        ev.target.disabled = true;
        ignoreAllWorth();
        return;
      }}
      var btn = ev.target.closest("[data-worth-page]");
      if (!btn || btn.disabled) return;
      if (!worthBody.contains(btn)) return;
      var next = parseInt(btn.getAttribute("data-worth-page"), 10);
      if (!next || next === worthPage) return;
      worthPage = next;
      persistWorthState();
      requestWorthRefresh();
    }});
    worthBody.addEventListener("keydown", function (ev) {{
      if (ev.key !== "Enter") return;
      if (!ev.target || ev.target.id !== "worth-min-match") return;
      ev.preventDefault();
      var applyBtn = document.getElementById("worth-apply-filter");
      if (applyBtn) applyBtn.click();
    }});
  }}
  function refresh() {{
    if (inFlight || document.hidden) return;
    inFlight = true;
    fetch(
      "/live?worth_page=" + encodeURIComponent(worthPage)
        + "&worth_min_match=" + encodeURIComponent(worthMinMatch),
      {{ headers: {{ Accept: "application/json" }} }}
    )
      .then(function (response) {{ return response.ok ? response.json() : Promise.reject(); }})
      .then(function (data) {{
        var stateEl = document.getElementById("collector-state");
        var messageEl = document.getElementById("runtime-message");
        var statsEl = document.getElementById("job-stats");
        var jobsEl = document.getElementById("jobs-body");
        var historyEl = document.getElementById("run-history");
        var logsEl = document.getElementById("logs-body");
        var queueEl = document.getElementById("queue-body");
        var worthEl = document.getElementById("worth-body");
        if (stateEl && stateEl.textContent !== data.state_label) stateEl.textContent = data.state_label;
        if (messageEl && messageEl.textContent !== data.message) messageEl.textContent = data.message;
        if (data.state) collectorState = data.state;
        if (typeof data.next_run_at !== "undefined") {{
          nextRunAtIso = data.next_run_at || null;
          updateNextRunTimer();
        }}
        if (data.worth_page) {{
          worthPage = data.worth_page;
          persistWorthState();
        }}
        if (typeof data.worth_min_match !== "undefined") {{
          worthMinMatch = data.worth_min_match;
          persistWorthState();
        }}
        applyRegion(statsEl, data.stats_html, data.stats_hash, {{ key: "stats" }});
        applyRegion(jobsEl, data.jobs_html, data.jobs_hash, {{ key: "jobs", skipIfBusy: true, wrapScroll: ".table-wrap" }});
        applyRegion(historyEl, data.history_html, data.history_hash, {{ key: "history" }});
        applyRegion(queueEl, data.queue_html, data.queue_hash, {{ key: "queue", skipIfBusy: true }});
        applyRegion(worthEl, data.worth_html, data.worth_hash, {{ key: "worth", skipIfBusy: !worthForceUpdate }});
        worthForceUpdate = false;
        applyRegion(
          document.getElementById("linkedin-filter-slot"),
          data.linkedin_filter_html,
          data.linkedin_filter_hash,
          {{ key: "linkedin_filter" }}
        );
        if (data.resume_status) {{
          ["pt", "en"].forEach(function (lang) {{
            var part = data.resume_status[lang];
            if (!part) return;
            applyRegion(document.getElementById("resume-meta-" + lang), part.meta_html, part.meta_hash, {{
              key: "resume-meta-" + lang
            }});
            applyRegion(document.getElementById("resume-dossier-" + lang), part.dossier_html, part.dossier_hash, {{
              key: "resume-dossier-" + lang,
              skipIfBusy: true,
              scrollSelector: ".resume-dossier-scroll",
              preserveDetails: true
            }});
          }});
        }}
        if (logsEl && data.logs_html && data.logs_hash && appliedHashes.logs !== data.logs_hash) {{
          if (!panelBusy(logsEl)) {{
            var stickBottom = logsEl.scrollTop + logsEl.clientHeight >= logsEl.scrollHeight - 40;
            var savedTop = logsEl.scrollTop;
            logsEl.innerHTML = data.logs_html;
            appliedHashes.logs = data.logs_hash;
            logsEl.scrollTop = stickBottom ? 0 : savedTop;
          }}
        }}
      }})
      .catch(function () {{}})
      .then(function () {{ inFlight = false; worthForceUpdate = false; }});
  }}
  var noticeTimer = null;
  var PROCESS_PATHS = {{
    "/upload-resume": 1,
    "/reanalyze-resume": 1,
    "/start": 1,
    "/stop": 1,
    "/queue-cancel": 1,
    "/queue-retry": 1,
    "/queue-retry-all": 1,
    "/queue-eval-new": 1,
    "/queue-clear": 1,
    "/clear-logs": 1,
    "/worth-easy-apply": 1,
    "/job-status": 1,
    "/worth-ignore-all": 1
  }};
  function noticeClass(kind) {{
    return {{
      success: "notice notice-ok",
      info: "notice notice-info",
      error: "notice notice-error",
      warning: "notice notice-warn"
    }}[kind] || "notice";
  }}
  function showNotice(text, kind) {{
    var el = document.getElementById("panel-notice");
    if (!el) return;
    el.className = noticeClass(kind || "info");
    el.textContent = text || "";
    el.hidden = !text;
    if (noticeTimer) clearTimeout(noticeTimer);
    if (text) {{
      noticeTimer = setTimeout(function () {{ el.hidden = true; }}, 6000);
    }}
  }}
  document.addEventListener("submit", function (ev) {{
    var form = ev.target;
    if (!(form instanceof HTMLFormElement)) return;
    var action = form.getAttribute("action") || "";
    var path = action.split("?")[0];
    if (!PROCESS_PATHS[path]) return;
    ev.preventDefault();
    var submitter = ev.submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (submitter) submitter.disabled = true;
    var body = new FormData(form);
    // multiplos botoes name=status (aplicada/ignorar/salvar): incluir o clicado
    if (submitter && submitter.name) {{
      body.set(submitter.name, submitter.value);
    }}
    fetch(path, {{
      method: "POST",
      body: body,
      headers: {{ Accept: "application/json", "X-Requested-With": "fetch" }},
      credentials: "same-origin"
    }})
      .then(function (r) {{ return r.json().then(function (data) {{ return {{ okHttp: r.ok, data: data }}; }}); }})
      .then(function (res) {{
        var data = res.data || {{}};
        showNotice(data.notice || (data.ok ? "OK" : "Falha"), data.kind || (data.ok ? "success" : "error"));
        if (path === "/upload-resume" && data.ok !== false) {{
          var fileInput = form.querySelector('input[type="file"]');
          if (fileInput) fileInput.value = "";
        }}
        if (path === "/job-status" || path === "/worth-ignore-all" || path === "/worth-easy-apply") {{
          requestWorthRefresh();
        }} else {{
          refresh();
        }}
      }})
      .catch(function () {{
        showNotice("Falha de comunicação com o painel.", "error");
      }})
      .then(function () {{
        if (submitter) submitter.disabled = false;
      }});
  }});
  setInterval(refresh, 1500);
  setInterval(updateNextRunTimer, 1000);
  updateNextRunTimer();
  document.addEventListener("visibilitychange", function () {{ if (!document.hidden) refresh(); }});
}})();
</script></body></html>'''


def parse_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    """Parse POST fields from urlencoded or multipart (fetch FormData uses multipart)."""
    content_type = (handler.headers.get("Content-Type") or "").casefold()
    if "multipart/form-data" in content_type:
        fields, _files = parse_multipart(handler)
        return fields
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def parse_multipart(handler: BaseHTTPRequestHandler) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Parseia multipart/form-data em campos de texto e arquivos (nome, bytes)."""
    content_type = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)
    if "boundary=" not in content_type:
        raise ValueError("Upload multipart inválido.")
    boundary = content_type.split("boundary=", 1)[1].strip().encode("utf-8")
    if boundary.startswith(b'"') and boundary.endswith(b'"'):
        boundary = boundary[1:-1]
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in body.split(b"--" + boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if part.startswith(b"--"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_blob, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = header_blob.decode("utf-8", errors="replace")
        disposition = ""
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                disposition = line
        name_match = re.search(r'name="([^"]+)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        if filename_match is not None:
            filename = filename_match.group(1) or "upload.pdf"
            files[name] = (filename, content)
        else:
            fields[name] = content.decode("utf-8", errors="replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    """Trata as ações do painel sem depender de um servidor web externo."""
    def send_page(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, notice: str = "", notice_kind: str = "success") -> None:
        body = render_page(notice, notice_kind=notice_kind)
        self.send_page(body)

    def wants_json(self) -> bool:
        return request_wants_json(self.headers)

    def respond_notice(
        self,
        notice: str = "",
        notice_kind: str = "success",
        *,
        ok: bool = True,
        status: int = 200,
    ) -> None:
        if self.wants_json():
            self.send_json({"ok": ok, "notice": notice, "kind": notice_kind}, status=status)
            return
        self.redirect(notice, notice_kind=notice_kind)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/live":
            qs = parse_qs(parsed.query)
            worth_page = 1
            worth_min_match = 0
            raw_page = (qs.get("worth_page") or ["1"])[0]
            raw_min = (qs.get("worth_min_match") or ["0"])[0]
            try:
                worth_page = max(1, int(raw_page))
            except ValueError:
                worth_page = 1
            try:
                worth_min_match = max(0, min(100, int(raw_min)))
            except ValueError:
                worth_min_match = 0
            self.send_json(live_payload(worth_page=worth_page, worth_min_match=worth_min_match))
            return
        self.send_page(render_page())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/upload-resume":
            try:
                fields, files = parse_multipart(self)
                language = (fields.get("language") or "").strip().casefold()
                if "file" not in files:
                    raise ValueError("Selecione um arquivo PDF.")
                filename, raw = files["file"]
                force = (fields.get("force_reanalyze") or "").strip() == "1"
                with connect() as db:
                    message, kind, needs_analysis = persist_resume_upload(
                        db,
                        language=language,
                        original_filename=filename,
                        raw_bytes=raw,
                        resumes_dir=RESUMES_DIR,
                        now_iso=now_iso(),
                        force_reanalyze=force,
                    )
                if needs_analysis:
                    # Force always creates a fresh queue job (no dedupe reuse).
                    qid = queue.enqueue(
                        KIND_RESUME,
                        {"language": language, "force": True, "requested_at": now_iso()},
                        dedupe_key=None if force else f"resume:{language}",
                    )
                    message = f"{message} Job de fila #{qid}."
                log_event(kind if kind != "info" else "info", "resume", message)
                self.respond_notice(message, notice_kind=kind)
            except Exception as exc:
                log_event("error", "resume", f"Falha no upload: {exc}")
                self.respond_notice(f"Falha no upload do currículo: {exc}", notice_kind="error", ok=False)
            return

        form = parse_form(self)
        if path == "/reanalyze-resume":
            language = (form.get("language") or "").strip().casefold()
            if language not in {"pt", "en"}:
                self.respond_notice("Idioma inválido para reanálise.", notice_kind="error", ok=False)
                return
            with connect() as db:
                row = get_resume(db, language)
                if not row:
                    self.respond_notice(f"Não há currículo {language.upper()} salvo.", notice_kind="error", ok=False)
                    return
                if not (row["extracted_text"] or "").strip() and not (
                    row["stored_path"] and os.path.exists(row["stored_path"])
                ):
                    self.respond_notice(
                        f"Currículo {language.upper()} sem arquivo/texto para reanalisar. Envie o PDF novamente.",
                        notice_kind="error",
                        ok=False,
                    )
                    return
                db.execute(
                    """UPDATE resumes SET analysis_status='pending', analysis_error='',
                           analysis_message=?, updated_at=? WHERE language=?""",
                    (
                        f"Currículo {language.upper()}: reanálise forçada — na fila.",
                        now_iso(),
                        language,
                    ),
                )
                actives = db.execute(
                    """SELECT id FROM queue_jobs
                       WHERE kind=? AND status IN ('pending','running','retry_wait')
                         AND payload LIKE ?""",
                    (KIND_RESUME, f'%"language": "{language}"%'),
                ).fetchall()
            for active in actives:
                queue.cancel(int(active["id"]))
            # Sem dedupe: sempre cria job novo para garantir nova chamada à IA.
            qid = queue.enqueue(
                KIND_RESUME,
                {"language": language, "force": True, "requested_at": now_iso()},
                dedupe_key=None,
            )
            msg = f"Reanálise do currículo {language.upper()} enfileirada (job #{qid}). Acompanhe em Filas/Logs."
            log_event("info", "resume", msg)
            self.respond_notice(msg, notice_kind="info")
            return

        if path == "/settings":
            if "apify_actors_json" in form and form.get("apify_actors_json", "").strip():
                try:
                    # Validate before persist.
                    load_apify_actors({**settings(), "apify_actors_json": form["apify_actors_json"]})
                except ValueError as exc:
                    self.redirect(f"Actors Apify inválidos: {exc}", notice_kind="error")
                    return
            save_settings(form)
            log_event("info", "settings", "Preferências de busca salvas.")
            self.redirect("Filtros salvos.")
        elif path == "/candidate-settings":
            save_settings(form)
            log_event("info", "settings", "Perfil do candidato salvo (dados, salário, diversidade).")
            self.redirect("Perfil salvo.")
        elif path == "/ai-settings":
            # Assisted-only: strip any legacy auto-submit flag if present in DB form posts.
            form.pop("linkedin_stop_before_submit", None)
            save_settings(form)
            provider = form.get("ai_provider", "gemini").casefold()
            try:
                if form.get("api_key", "").strip():
                    save_ai_key(provider, form.get("api_key", ""))
                secret_set("adzuna_app_id", form.get("adzuna_app_id", ""))
                secret_set("adzuna_app_key", form.get("adzuna_app_key", ""))
                secret_set("apify_token", form.get("apify_token", ""))
            except RuntimeError as exc:
                log_event("error", "settings", str(exc))
                self.redirect(str(exc), notice_kind="error")
                return
            log_event("info", "settings", f"IA/integrações salvas (provedor={provider}).")
            self.redirect("IA e integrações salvas.")
        elif path == "/automation-settings":
            if "auto_apply" not in form:
                form["auto_apply"] = "0"
            if "linkedin_easy_apply" not in form:
                form["linkedin_easy_apply"] = "0"
            if "linkedin_risk_ack" not in form:
                form["linkedin_risk_ack"] = "0"
            # Assisted-only: strip legacy flags if present in form posts.
            form.pop("linkedin_stop_before_submit", None)
            form.pop("linkedin_max_per_day", None)
            form.pop("inhire_pcd", None)
            save_settings(form)
            log_event(
                "info",
                "settings",
                f"Automação salva (auto_apply={form.get('auto_apply')}, "
                f"linkedin_easy_apply={form.get('linkedin_easy_apply')}, "
                f"risk_ack={form.get('linkedin_risk_ack')}).",
            )
            self.redirect("Automação salva.")
        elif path == "/smtp-settings":
            save_settings({k: form.get(k, "") for k in ("smtp_host", "smtp_port", "smtp_user", "smtp_from", "smtp_use_tls")})
            try:
                if form.get("smtp_password", "").strip():
                    secret_set("smtp_password", form["smtp_password"])
            except RuntimeError as exc:
                self.redirect(str(exc), notice_kind="error")
                return
            log_event("info", "smtp", "Configuração SMTP salva.")
            self.redirect("SMTP salvo.")
        elif path == "/smtp-test":
            cfg = settings()
            to_addr = (cfg.get("candidate_email") or cfg.get("smtp_from") or "").strip()
            if not to_addr:
                self.redirect("Configure candidate_email ou smtp_from para testar.", notice_kind="warning")
                return
            try:
                send_smtp_email(
                    cfg=cfg,
                    password=secret_get("smtp_password"),
                    to_addrs=[to_addr],
                    subject="Radar de Vagas — teste SMTP",
                    body="Este é um e-mail de teste do Radar de Vagas.",
                    attachment_path=None,
                )
                log_event("success", "smtp", f"E-mail de teste enviado para {to_addr}.")
                self.redirect(f"E-mail de teste enviado para {to_addr}.")
            except Exception as exc:
                log_event("error", "smtp", f"Falha no teste SMTP: {exc}")
                self.redirect(f"Falha no teste SMTP: {exc}", notice_kind="error")
        elif path == "/clear-logs":
            n = clear_logs()
            log_event("info", "logs", f"Histórico limpo ({n} entradas removidas).")
            self.respond_notice("Logs limpos.", notice_kind="info")
        elif path == "/queue-cancel":
            jid = form.get("id", "")
            if jid.isdigit() and queue.cancel(int(jid)):
                self.respond_notice(f"Job #{jid} cancelado.", notice_kind="warning")
            else:
                self.respond_notice("Não foi possível cancelar o job.", notice_kind="error", ok=False)
        elif path == "/queue-retry":
            jid = form.get("id", "")
            if jid.isdigit() and queue.retry_now(int(jid)):
                self.respond_notice(f"Job #{jid} reenfileirado.", notice_kind="info")
            else:
                self.respond_notice("Não foi possível reenfileirar o job.", notice_kind="error", ok=False)
        elif path == "/queue-retry-all":
            n = queue.retry_all()
            if n:
                self.respond_notice(f"{n} job(s) reenfileirado(s).", notice_kind="info")
            else:
                self.respond_notice("Nenhum job elegível para reenfileirar.", notice_kind="warning")
        elif path == "/queue-eval-new":
            note = process_auto_apply_batch()
            kind = "error" if note == "auto_apply desligado" else ("warning" if "nenhuma" in note else "info")
            self.respond_notice(note, notice_kind=kind, ok=kind != "error")
        elif path == "/queue-clear":
            n = queue.clear_terminal()
            self.respond_notice(f"{n} job(s) removido(s) da fila.", notice_kind="info")
        elif path == "/profile-settings":
            save_settings(form)
            with connect() as db:
                save_rules_from_form(db, form)
            log_event("info", "forms", "Regras de formulário salvas.")
            self.redirect("Regras de formulário salvas.")
        elif path == "/start":
            started = collector.start()
            self.respond_notice("Coleta iniciada." if started else "A coleta já está em execução.")
        elif path == "/stop":
            collector.stop()
            self.respond_notice("Solicitação para parar enviada.")
        elif path == "/worth-easy-apply":
            job_id = int(form["id"]) if form.get("id", "").isdigit() else 0
            if not job_id:
                self.respond_notice("ID da vaga inválido.", notice_kind="error", ok=False)
                return
            note = enqueue_worth_easy_apply(job_id)
            kind = (
                "error"
                if note.startswith("Ative")
                or note.startswith("Confirme")
                or note.startswith("Só")
                or note.startswith("Esta")
                or note.startswith("Vaga não")
                else "info"
            )
            self.respond_notice(note, notice_kind=kind, ok=kind != "error")
        elif path == "/job-status":
            if form.get("status") in STATUSES and form.get("id", "").isdigit():
                with connect() as db:
                    applied = now_iso() if form["status"] == "applied" else None
                    db.execute(
                        "UPDATE jobs SET status=?, applied_at=COALESCE(?, applied_at) WHERE id=?",
                        (form["status"], applied, int(form["id"])),
                    )
                labels = {"applied": "marcada como aplicada", "ignored": "ignorada", "saved": "salva"}
                label = labels.get(form["status"], "atualizada")
                self.respond_notice(f"Vaga {label}.")
            else:
                self.respond_notice("Pedido inválido.", notice_kind="error", ok=False)
        elif path == "/worth-ignore-all":
            with connect() as db:
                cur = db.execute("UPDATE jobs SET status='ignored' WHERE status='worth'")
                n = cur.rowcount if cur.rowcount is not None else 0
            if n <= 0:
                self.respond_notice("Nenhuma vaga em Vale a pena olhar para ignorar.", notice_kind="info")
            elif n == 1:
                self.respond_notice("1 vaga ignorada.")
            else:
                self.respond_notice(f"{n} vagas ignoradas.")
        elif path == "/notes":
            if form.get("id", "").isdigit():
                with connect() as db:
                    db.execute("UPDATE jobs SET notes=? WHERE id=?", (form.get("notes", ""), int(form["id"])))
            self.redirect("Anotação salva.")
        else:
            self.send_page("Não encontrado", 404)

    #: endpoints de polling — o browser bate a cada poucos segundos; sem isso
    #: o terminal/LOG enche de linha repetida e sota o que importa.
    QUIET_PATHS = ("/live", "/favicon.ico")

    def log_message(self, fmt: str, *args: object) -> None:
        line = fmt % args
        if any(p in line for p in self.QUIET_PATHS):
            LOG.debug("%s - %s", self.address_string(), line)
            return
        LOG.info("%s - %s", self.address_string(), line)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    initialize()
    print(f"Radar de Vagas disponível em http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
