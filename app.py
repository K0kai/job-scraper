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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from apply_channels import apply_via_browser, apply_via_email, record_blocked, send_smtp_email
from form_rules import ensure_default_rules, list_rules, save_rules_from_form
from job_queue import KIND_APPLY, KIND_RESUME, JobQueue
from resume_pipeline import (
    AiUnavailableError,
    get_resume,
    mark_resume_analysis_error,
    persist_resume_upload,
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
    "candidate_profile_pt": "",
    "candidate_profile_en": "",
    "candidate_facts_pt": "",
    "candidate_facts_en": "",
    "resume_pt_path": "",
    "resume_en_path": "",
    "auto_apply": "0",
    "minimum_match_score": "80",
    "maximum_applications_per_run": "5",
    "adzuna_countries": "br,us,gb,ca",
    "apify_monthly_credit_limit_usd": "5",
    "apify_job_count": "25",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_from": "",
    "smtp_use_tls": "1",
    "queue_max_workers": "3",
    "queue_max_attempts": "40",
    "queue_ttl_hours": "24",
}
STATUSES = {"new": "Nova", "review": "Revisar", "saved": "Salva", "prepared": "Carta preparada", "applied": "Aplicada", "ignored": "Ignorada", "blocked": "Envio indisponível"}
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
    if "T" not in raw and " " not in raw:
        return raw[:10]
    parsed = parse_utc(raw)
    if parsed is None:
        return raw
    return parsed.astimezone(BRASILIA).strftime("%d/%m/%Y %H:%M")


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    return db


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
    resume_summary_pt = summaries.get("pt", "").strip()
    resume_summary_en = summaries.get("en", "").strip()
    if language == "pt" and not resume_summary_pt and not resume_summary_en:
        raise ValueError("Faça upload e análise de pelo menos um currículo antes da triagem automática.")
    if language == "en" and not resume_summary_en and not resume_summary_pt:
        raise ValueError("Faça upload e análise de pelo menos um currículo antes da triagem automática.")

    prompt = f"""Evaluate whether this candidate should apply to this job. Use only the candidate facts below and resume summary in the appropriate language; do not infer missing qualifications. Consider explicit location/work authorization, seniority, required skills, and role fit. Identify whether the job description asks for a cover letter. Return only JSON with keys: match_score (integer 0-100), should_apply (boolean), cover_letter_required (boolean), reason (short string in {('Portuguese' if language == 'pt' else 'English')}), recommended_resume_language (either "en" or "pt").
Candidate facts: {facts or '[none provided]'}
Resume summary (PT): {resume_summary_pt or '[none provided]'}
Resume summary (EN): {resume_summary_en or '[none provided]'}
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
            payload = json.dumps({"model": model, "input": prompt, "text": {"format": {"type": "json_object"}}, "store": False, "max_output_tokens": 300}).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == "gemini":
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
            payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 300, "responseMimeType": "application/json"}}).encode("utf-8")
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
        if language == "en" and match_score >= 80 and resume_summary_en:
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
    return {
        "match_score": max(0, min(100, int(decision.get("match_score", 0)))),
        "should_apply": bool(decision.get("should_apply")),
        "letter_required": bool(decision.get("cover_letter_required")),
        "reason": str(decision.get("reason", ""))[:1000],
        "provider": provider,
        "model": model,
        "resume_language": recommended_language,
    }


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

    with connect() as db:
        ok, detail = apply_via_email(
            db,
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
                db,
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
            record_blocked(db, job_id, detail, now_iso(), int(resume["id"]), cover_id)
            db.execute("UPDATE jobs SET status='blocked', notes=? WHERE id=?", (detail[:900], job_id))
            db.execute(
                "UPDATE ai_decisions SET apply_channel=?, apply_result=? WHERE id=(SELECT MAX(id) FROM ai_decisions WHERE job_id=?)",
                (channel, detail[:500], job_id),
            )
            log_event("warning", "auto-apply", f"Vaga #{job_id} bloqueada via {channel}: {detail}")
            return f"vaga {job_id}: bloqueada — {detail}"
        db.execute("UPDATE jobs SET status='applied', applied_at=?, notes=? WHERE id=?", (now_iso(), detail[:900], job_id))
        db.execute(
            "UPDATE ai_decisions SET apply_channel=?, apply_result=? WHERE id=(SELECT MAX(id) FROM ai_decisions WHERE job_id=?)",
            (channel, detail[:500], job_id),
        )
    log_event("success", "auto-apply", f"Vaga #{job_id} aplicada via {channel}: {detail}")
    return f"vaga {job_id}: aplicada via {channel} — {detail}"


def process_auto_apply_batch() -> str:
    """Enfileira vagas novas para triagem/candidatura assíncrona com retry."""
    cfg = settings()
    if cfg.get("auto_apply") != "1":
        return "auto_apply desligado"
    try:
        limit = max(1, min(50, int(cfg.get("maximum_applications_per_run", "5") or 5)))
    except ValueError:
        limit = 5
    with connect() as db:
        rows = db.execute("SELECT id FROM jobs WHERE status='new' ORDER BY id ASC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "nenhuma vaga nova para enfileirar"
    queued = 0
    for row in rows:
        job_id = int(row["id"])
        queue.enqueue(KIND_APPLY, {"job_id": job_id}, dedupe_key=f"apply:{job_id}")
        with connect() as db:
            db.execute(
                "UPDATE jobs SET status='review', notes=? WHERE id=? AND status='new'",
                ("Na fila de candidatura (retry automático se a IA estiver ocupada).", job_id),
            )
        queued += 1
    return f"{queued} vaga(s) enfileirada(s) para triagem/candidatura"


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


queue = JobQueue(
    db_path=DB_PATH,
    handlers={KIND_RESUME: handle_resume_analysis_job, KIND_APPLY: handle_job_apply_job},
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


APIFY_ACTOR_ID = "curious_coder~linkedin-jobs-scraper"
APIFY_API = "https://api.apify.com/v2"


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


def fetch_apify() -> list[dict]:
    """Roda o actor de job scraping da Apify sem substituir as outras fontes."""
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
    try:
        count = max(1, min(100, int(cfg.get("apify_job_count", "25"))))
    except ValueError:
        count = 25
    keywords = terms(cfg.get("keywords", ""))[:2] or ["software engineer"]
    locations = terms(cfg.get("locations", ""))[:2] or ["remote"]
    urls: list[str] = []
    for keyword in keywords:
        for location in locations:
            urls.append(f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(keyword)}&location={quote_plus(location)}&position=1&pageNum=0")
            if len(urls) >= 2:
                break
        if len(urls) >= 2:
            break
    run_input = {"urls": urls, "count": count, "scrapeCompany": False}
    started = fetch_json(
        f"{APIFY_API}/actors/{APIFY_ACTOR_ID}/runs",
        data=json.dumps(run_input).encode("utf-8"),
        headers=apify_headers(token),
        timeout=45,
    )
    run = started.get("data", started) if isinstance(started, dict) else {}
    run_id = str(run.get("id") or "")
    dataset_id = str(run.get("defaultDatasetId") or "")
    if not run_id:
        raise RuntimeError("A Apify não retornou o identificador da execução.")
    deadline = time.time() + 180
    status = str(run.get("status") or "READY")
    while status in {"READY", "RUNNING"} and time.time() < deadline:
        if collector.stop_event.is_set():
            raise RuntimeError("Coleta interrompida durante a execução Apify.")
        time.sleep(5)
        progress = fetch_json(f"{APIFY_API}/actor-runs/{quote_plus(run_id)}", headers=apify_headers(token), timeout=30)
        run = progress.get("data", progress) if isinstance(progress, dict) else {}
        status = str(run.get("status") or status)
        dataset_id = str(run.get("defaultDatasetId") or dataset_id)
    if status != "SUCCEEDED":
        raise RuntimeError(f"Execução Apify encerrada com status {status}.")
    if not dataset_id:
        raise RuntimeError("A execução Apify não produziu um dataset.")
    items = fetch_json(f"{APIFY_API}/datasets/{quote_plus(dataset_id)}/items?clean=1", headers=apify_headers(token), timeout=45)
    if not isinstance(items, list):
        items = []
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("link") or item.get("url") or item.get("applyUrl") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        description = str(item.get("descriptionText") or item.get("descriptionHtml") or item.get("description") or "")
        results.append({
            "source": "Apify",
            "source_id": str(item.get("id") or url),
            "title": title,
            "company": str(item.get("companyName") or item.get("company") or ""),
            "location": str(item.get("location") or ""),
            "description": description,
            "url": url,
            "posted_at": item.get("postedAt") or item.get("publishedAt"),
        })
    try:
        used_after, cycle_after = apify_monthly_usage_usd(token)
        set_setting("apify_last_usage_usd", f"{used_after:.4f}")
        set_setting("apify_last_usage_cycle", cycle_after)
    except Exception:
        pass
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

    def snapshot(self) -> dict:
        with self.lock:
            return {"state": self.state, "message": self.message, "last_run": self.last_run, "last_found": self.last_found}

    def start(self) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self.stop_event.clear()
            self.state = "running"
            self.message = "Iniciando busca…"
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
            self._run_once()
            cfg = settings()
            try:
                seconds = max(1, int(cfg.get("interval_minutes", "15"))) * 60
            except ValueError:
                seconds = POLL_SECONDS
            if self.stop_event.wait(seconds):
                break
        with self.lock:
            self.state = "stopped"
            self.message = "Coleta parada"
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
    return "".join(f'<div class="stat"><span>{esc(label)}</span><strong>{counts.get(key, 0)}</strong></div>' for key, label in [("new", "Novas"), ("review", "Revisar"), ("prepared", "Preparadas"), ("applied", "Aplicadas"), ("ignored", "Ignoradas")])


def job_rows_html(jobs, collecting: bool) -> str:
    rows = []
    for job in jobs:
        description = re.sub(r"<[^>]+>", " ", job["description"])
        lang_label = {"pt": "Português", "en": "English", "unknown": "Incerto"}.get(job["language"], job["language"])
        confidence = f" · {job['language_confidence']:.0%}" if job["language_confidence"] else ""
        letter_ui = f'<details><summary>{"Carta criada" if job["cover_letter"] else "Carta não criada"}</summary><div class="description">{esc(job["cover_letter"] or "A carta será gerada automaticamente na candidatura.")}</div></details>'
        notes = f'<details><summary>Notas</summary><div class="description">{esc(job["notes"] or "—")}</div></details>' if job["notes"] else ""
        rows.append(f'''<tr><td><a class="job-title" href="{esc(job['url'])}" target="_blank" rel="noreferrer">{esc(job['title'])}</a><small>{esc(job['company'])}</small></td><td>{esc(job['location'])}</td><td><span class="source">{esc(job['source'])}</span></td><td><span title="Confiança do detector: {confidence}">{esc(lang_label + confidence)}</span></td><td>{esc(STATUSES.get(job['status'], job['status']))}</td><td>{letter_ui}{notes}<details><summary>Descrição</summary><div class="description">{esc(description[:1800])}</div></details></td><td><small>{esc(format_brasilia(job['posted_at'] or job['first_seen_at']))}</small></td></tr>''')
    if rows:
        return "".join(rows)
    if collecting:
        return '<tr><td colspan="7">Buscando vagas… as novas aparecem aqui assim que forem encontradas.</td></tr>'
    return '<tr><td colspan="7">Nenhuma vaga ainda. Configure as fontes e inicie o bot.</td></tr>'


def history_html(runs) -> str:
    return "".join(f'<li><span>{esc(run["started_at"][:16].replace("T", " "))}</span> {esc(run["message"] or run["state"])}</li>' for run in runs) or "<li>Nenhuma execução ainda.</li>"


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
        f'failed={counts.get("failed",0)} · cancelled={counts.get("cancelled",0)}. Workers: {esc(settings().get("queue_max_workers","3"))}.</p>'
    )
    if not rows:
        return summary + '<p class="hint">Nenhum job na fila ainda.</p>'
    kind_label = {KIND_RESUME: "Análise de currículo", KIND_APPLY: "Candidatura"}
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
            actions.append(f'<form method="post" action="/queue-cancel" style="display:inline"><input type="hidden" name="id" value="{jid}"><button class="subtle" type="submit">Cancelar</button></form>')
        if row["status"] in {"retry_wait", "failed", "cancelled"}:
            actions.append(f'<form method="post" action="/queue-retry" style="display:inline"><input type="hidden" name="id" value="{jid}"><button class="subtle" type="submit">Reenfileirar</button></form>')
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
    clear_btn = '<form method="post" action="/queue-clear" style="margin-top:10px"><button class="subtle" type="submit">Limpar concluídos/falhos/cancelados</button></form>'
    return summary + table + clear_btn


def state_label_for(state: str) -> str:
    return {"running": "Em execução", "stopping": "Parando", "stopped": "Parada"}.get(state, state)


def live_payload() -> dict:
    status = collector.snapshot()
    counts, jobs, runs = load_dashboard()
    collecting = status["state"] in {"running", "stopping"}
    return {
        "state": status["state"],
        "state_label": state_label_for(status["state"]),
        "message": status["message"],
        "stats_html": stats_html(counts),
        "jobs_html": job_rows_html(jobs, collecting),
        "history_html": history_html(runs),
        "logs_html": logs_html(),
        "queue_html": queue_html(),
    }



def resume_panels_html() -> str:
    with connect() as db:
        resumes = {row["language"]: row for row in db.execute("SELECT * FROM resumes")}
    blocks = []
    for lang, label in (("pt", "Português"), ("en", "English")):
        row = resumes.get(lang)
        if not row:
            status = (
                '<div class="analysis-badge analysis-none">Sem PDF</div>'
                '<p class="hint">Nenhum currículo enviado. Escolha um PDF e clique em enviar para a IA analisar.</p>'
            )
        else:
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
            summary = esc((row["analysis_summary"] or "")[:900])
            error_block = (
                f'<p class="analysis-error-text"><strong>Detalhe do erro:</strong> {error}</p>'
                if error and raw_status == "error"
                else ""
            )
            summary_block = (
                f'<details open><summary>Resumo da análise da IA</summary><div class="description">{summary}</div></details>'
                if summary
                else '<p class="hint">Ainda não há resumo salvo (análise incompleta ou falhou).</p>'
            )
            status = (
                f'<div class="analysis-badge {badge_class}">{badge_label}</div>'
                f'<p class="hint"><strong>Arquivo:</strong> {filename}<br>'
                f'<strong>Última análise:</strong> {when}<br>'
                f'<strong>Modelo:</strong> {provider} / {model}</p>'
                f'<p class="hint">{message}</p>'
                f"{error_block}{summary_block}"
            )
        blocks.append(
            f'<div class="resume-card"><h3 style="margin:0 0 8px;font-size:15px">Currículo {label}</h3>'
            f'<form method="post" action="/upload-resume" enctype="multipart/form-data">'
            f'<input type="hidden" name="language" value="{lang}">'
            f'<label>Selecionar PDF<input type="file" name="file" accept="application/pdf" required></label>'
            f'<button style="margin-top:8px">Enviar e analisar</button></form>{status}</div>'
        )
    return '<div class="form-grid">' + "".join(blocks) + "</div>"


def form_rules_html() -> str:
    with connect() as db:
        rules = list_rules(db)
    rows = []
    for idx, rule in enumerate(rules, start=1):
        mode_opts = "".join(
            f'<option value="{m}" {"selected" if rule["mode"] == m else ""}>{m}</option>'
            for m in ("text", "select", "file", "cover_letter", "skip")
        )
        rows.append(
            f'<tr><td><input name="rule_key_{idx}" value="{esc(rule["key"])}"></td>'
            f'<td><input name="rule_aliases_{idx}" value="{esc(rule["aliases"])}"></td>'
            f'<td><select name="rule_mode_{idx}">{mode_opts}</select></td>'
            f'<td><input name="rule_value_from_{idx}" value="{esc(rule["value_from"])}" placeholder="ex.: candidate_phone"></td>'
            f'<td><input name="rule_value_{idx}" value="{esc(rule["value"])}"></td></tr>'
        )
    idx = len(rules) + 1
    mode_opts = "".join(f'<option value="{m}">{m}</option>' for m in ("text", "select", "file", "cover_letter", "skip"))
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
    resume_panel = resume_panels_html()
    rules_panel = form_rules_html()
    logs_view = logs_html()
    queue_view = queue_html()
    notice_class = {
        "success": "notice notice-ok",
        "info": "notice notice-info",
        "error": "notice notice-error",
        "warning": "notice notice-warn",
    }.get(notice_kind, "notice")
    notice_html = f'<div class="{notice_class}">{esc(notice)}</div>' if notice else ""
    return f'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar de Vagas</title><style>
      :root{{--ink:#172b36;--muted:#62747d;--line:#dce5e8;--paper:#f4f7f7;--teal:#0b786d;--mint:#d8f0e9;--white:#fff}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 Inter,Segoe UI,Arial,sans-serif}}header{{background:#102d35;color:white;padding:28px max(24px,calc((100vw - 1280px)/2));display:flex;justify-content:space-between;align-items:center}}h1{{font-size:25px;margin:0}}header p{{margin:5px 0 0;color:#c1d4d6}}main{{max-width:1280px;margin:26px auto;padding:0 24px}}.top{{display:grid;grid-template-columns:1.3fr .7fr;gap:18px}}.panel,.stat,.table-wrap{{background:white;border:1px solid var(--line);border-radius:13px}}.panel{{padding:20px}}h2{{font-size:18px;margin:0 0 14px}}.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}label{{display:block;color:var(--muted);font-size:13px;font-weight:600}}input,textarea,select{{font:inherit;color:var(--ink);width:100%;margin-top:5px;padding:9px 10px;border:1px solid #cdd9dc;border-radius:8px;background:white}}.hint{{color:var(--muted);font-size:12px;margin:10px 0}}button{{border:0;border-radius:8px;padding:10px 15px;background:var(--teal);color:white;font-weight:650;cursor:pointer}}button.stop{{background:#a74639}}button.subtle{{padding:7px 10px;background:#eaf2f1;color:var(--ink);margin-top:6px}}.actions{{display:flex;gap:9px;margin-top:12px;align-items:center}}.runtime{{color:var(--muted);font-size:13px}}.stats{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:18px 0}}.stat{{padding:13px 15px}}.stat span{{display:block;font-size:12px;color:var(--muted)}}.stat strong{{font-size:23px}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:950px}}th,td{{padding:13px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{font-size:12px;color:var(--muted);background:#f8fafa}}td small{{display:block;color:var(--muted);margin-top:3px}}.job-title{{font-weight:700;color:#145d59;text-decoration:none}}.job-title:hover{{text-decoration:underline}}.source{{background:var(--mint);padding:3px 7px;border-radius:99px;font-size:12px}}select{{min-width:150px;margin:0;padding:7px}}summary{{cursor:pointer;color:var(--teal);font-size:13px}}.description{{max-width:350px;max-height:220px;overflow:auto;padding:8px 0;font-size:13px}}details textarea{{min-width:230px}}.history{{color:var(--muted);font-size:13px;padding-left:20px}}.notice{{padding:10px 13px;border-radius:8px;margin-bottom:15px}}.notice-ok{{background:#e7f4ed;border:1px solid #b7dfc8}}.notice-info{{background:#e8f1f8;border:1px solid #b7d0e6}}.notice-error{{background:#fceaea;border:1px solid #e3b0b0;color:#6b2a2a}}.notice-warn{{background:#fff6e5;border:1px solid #e6d0a0}}.analysis-badge{{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:700;margin:10px 0 6px}}.analysis-ok{{background:#d8f0e9;color:#0b5c52}}.analysis-info{{background:#dceaf6;color:#1d4f74}}.analysis-error{{background:#f6d6d6;color:#7a2424}}.analysis-none{{background:#eceff1;color:#526066}}.analysis-error-text{{color:#7a2424;font-size:13px;margin:8px 0}}.resume-card{{border:1px solid var(--line);border-radius:10px;padding:14px;background:#fbfcfc}}.tabs{{display:flex;gap:8px;margin:0 0 16px}}.tab{{background:#e7eeef;color:var(--ink);padding:9px 16px;border-radius:999px;font-weight:650;cursor:pointer}}.tab.active{{background:var(--teal);color:white}}.tab-panel{{display:none}}.tab-panel.active{{display:block}}.log-console{{font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#0f1c22;color:#d7e6ea;border-radius:10px;padding:12px;max-height:620px;overflow:auto}}.log-line{{display:grid;grid-template-columns:132px 72px 110px 1fr;gap:10px;padding:5px 0;border-bottom:1px solid #1e323b}}.log-time{{color:#8eacb6}}.log-level{{font-weight:700;text-transform:uppercase}}.log-source{{color:#7ec8c0}}.log-msg{{color:#e8f3f5;white-space:pre-wrap;word-break:break-word}}.log-info .log-level{{color:#9ec9ff}}.log-success .log-level{{color:#7ddea8}}.log-warning .log-level{{color:#f0c674}}.log-error .log-level{{color:#f0a0a0}}.log-debug .log-level{{color:#9aa7ad}}.log-empty{{color:#9bb0b8;padding:18px 8px}}.queue-status{{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px;font-weight:700}}.queue-pending,.queue-retry_wait{{background:#dceaf6;color:#1d4f74}}.queue-running{{background:#d8f0e9;color:#0b5c52}}.queue-succeeded{{background:#e7f4ed;color:#1f6b45}}.queue-failed{{background:#f6d6d6;color:#7a2424}}.queue-cancelled{{background:#eceff1;color:#526066}}@media(max-width:900px){{.log-line{{grid-template-columns:1fr;gap:2px}}}}@media(max-width:800px){{.top{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}header{{padding:20px 24px}}.form-grid{{grid-template-columns:1fr}}}}
      </style></head><body><header><div><h1>Radar de Vagas</h1><p>Busca, seleção e candidaturas automáticas</p></div><span id="collector-state">{esc(state_label)}</span></header><main>{notice_html}<nav class="tabs" aria-label="Seções do painel"><button type="button" class="tab active" data-tab="painel">Painel</button><button type="button" class="tab" data-tab="filas">Filas</button><button type="button" class="tab" data-tab="logs">Logs</button></nav><div id="tab-painel" class="tab-panel active"><div class="top"><section class="panel"><h2>Preferências de busca</h2><form method="post" action="/settings"><div class="form-grid"><label>Cargos e termos, separados por vírgula<textarea name="keywords" rows="3">{esc(cfg.get('keywords',''))}</textarea></label><label>Países/regiões aceitos<textarea name="locations" rows="3">{esc(cfg.get('locations',''))}</textarea></label><label>Fontes: remotive, remoteok, adzuna, apify<input name="sources" value="{esc(cfg.get('sources',''))}"></label><label>Intervalo de busca (minutos)<input name="interval_minutes" type="number" min="5" value="{esc(cfg.get('interval_minutes','15'))}"></label><label>Países Adzuna (ex.: br,us,gb,ca)<input name="adzuna_countries" value="{esc(cfg.get('adzuna_countries','br,us,gb,ca'))}"></label><label>Limite mensal Apify (USD)<input name="apify_monthly_credit_limit_usd" type="number" min="0" step="0.01" value="{esc(cfg.get('apify_monthly_credit_limit_usd','5'))}"></label><label>Máximo de vagas por ciclo Apify<input name="apify_job_count" type="number" min="1" max="100" value="{esc(cfg.get('apify_job_count','25'))}"></label></div><button>Salvar preferências</button></form><div class="actions"><form method="post" action="/start"><button>Iniciar bot</button></form><form method="post" action="/stop"><button class="stop">Parar bot</button></form><span class="runtime" id="runtime-message">{esc(status['message'])}</span></div></section><section class="panel"><h2>Execuções recentes</h2><ul class="history" id="run-history">{history}</ul></section></div><section class="panel" style="margin-top:18px"><h2>Currículos (PDF)</h2>{resume_panel}<p class="hint">O seletor de arquivos do sistema abre ao escolher o PDF. A IA analisa só quando o arquivo muda (hash). O status abaixo mostra se a análise ocorreu, foi reutilizada ou falhou.</p></section>
<section class="panel" style="margin-top:18px"><h2>Perfil, SMTP e automação</h2><form method="post" action="/ai-settings"><div class="form-grid"><label>Provedor de IA<select name="ai_provider"><option value="gemini" {'selected' if cfg.get('ai_provider') == 'gemini' else ''}>Gemini</option><option value="openai" {'selected' if cfg.get('ai_provider') == 'openai' else ''}>OpenAI</option></select></label><label>Modelo<input name="ai_model" value="{esc(cfg.get('ai_model','gemini-2.5-flash'))}"></label><label>Seu nome<input name="candidate_name" value="{esc(cfg.get('candidate_name',''))}"></label><label>E-mail<input name="candidate_email" value="{esc(cfg.get('candidate_email',''))}"></label><label>Telefone<input name="candidate_phone" value="{esc(cfg.get('candidate_phone',''))}"></label><label>LinkedIn<input name="candidate_linkedin" value="{esc(cfg.get('candidate_linkedin',''))}"></label><label>Cidade<input name="candidate_city" value="{esc(cfg.get('candidate_city',''))}"></label><label>Chave de IA (vazio mantém a salva)<input type="password" name="api_key" autocomplete="new-password"></label><label>Fatos profissionais em português<textarea name="candidate_facts_pt" rows="3">{esc(cfg.get('candidate_facts_pt',''))}</textarea></label><label>Professional facts in English<textarea name="candidate_facts_en" rows="3">{esc(cfg.get('candidate_facts_en',''))}</textarea></label><label>Score mínimo (%)<input name="minimum_match_score" type="number" min="0" max="100" value="{esc(cfg.get('minimum_match_score','80'))}"></label><label>Máximo de candidaturas por ciclo<input name="maximum_applications_per_run" type="number" min="1" max="50" value="{esc(cfg.get('maximum_applications_per_run','5'))}"></label><label>Workers da fila (paralelo)<input name="queue_max_workers" type="number" min="1" max="8" value="{esc(cfg.get('queue_max_workers','3'))}"></label><label>Máx. tentativas por job<input name="queue_max_attempts" type="number" min="1" max="200" value="{esc(cfg.get('queue_max_attempts','40'))}"></label><label>TTL da fila (horas)<input name="queue_ttl_hours" type="number" min="1" max="168" value="{esc(cfg.get('queue_ttl_hours','24'))}"></label><label>Adzuna App ID<input name="adzuna_app_id" value=""></label><label>Adzuna API key<input type="password" name="adzuna_app_key" value=""></label><label>Token Apify<input type="password" name="apify_token" value="" autocomplete="new-password"></label></div><label style="margin:12px 0"><input type="checkbox" name="auto_apply" value="1" {'checked' if cfg.get('auto_apply') == '1' else ''} style="width:auto"> Ativar triagem e candidatura automáticas (e-mail SMTP, depois formulário público)</label><p class="hint">Chaves ficam no cofre do sistema. Match usa a análise salva do currículo. Sem e-mail/formulário ou com pergunta aberta sem tokens de IA, a vaga é marcada como bloqueada/pulada.</p><button>Salvar perfil e IA</button></form></section>
<section class="panel" style="margin-top:18px"><h2>SMTP</h2><form method="post" action="/smtp-settings"><div class="form-grid"><label>Host<input name="smtp_host" value="{esc(cfg.get('smtp_host',''))}"></label><label>Porta<input name="smtp_port" type="number" value="{esc(cfg.get('smtp_port','587'))}"></label><label>Usuário<input name="smtp_user" value="{esc(cfg.get('smtp_user',''))}"></label><label>Remetente (From)<input name="smtp_from" value="{esc(cfg.get('smtp_from',''))}"></label><label>Senha (vazio mantém)<input type="password" name="smtp_password" autocomplete="new-password"></label><label>TLS<select name="smtp_use_tls"><option value="1" {'selected' if cfg.get('smtp_use_tls','1')=='1' else ''}>Sim (STARTTLS)</option><option value="0" {'selected' if cfg.get('smtp_use_tls')=='0' else ''}>Não</option></select></label></div><div class="actions"><button>Salvar SMTP</button></div></form><form method="post" action="/smtp-test" style="margin-top:8px"><button class="subtle" type="submit">Enviar e-mail de teste</button></form></section>
<section class="panel" style="margin-top:18px"><h2>Regras de formulário (Playwright)</h2><form method="post" action="/profile-settings">{rules_panel}<p class="hint">Para selects (ex.: salário), coloque em "Valor fixo" o texto da opção preferida. Perguntas abertas sem regra usam a IA; se não houver tokens, a vaga é pulada.</p><button>Salvar regras</button></form></section><div class="stats" id="job-stats">{cards}</div><section class="table-wrap"><table><thead><tr><th>Vaga</th><th>Localidade</th><th>Fonte</th><th>Idioma</th><th>Etapa</th><th>Carta e descrição</th><th>Data</th></tr></thead><tbody id="jobs-body">{rows}</tbody></table></section></div>
<div id="tab-filas" class="tab-panel"><section class="panel"><h2 style="margin-top:0">Filas de IA (async + retry)</h2><p class="hint">Análise de currículo e candidaturas rodam em paralelo (até 3 workers). Em fila/rate-limit da API, o job entra em retry automático até sucesso, expirar (24h) ou cancelar.</p><div id="queue-body">{queue_view}</div></section></div>
<div id="tab-logs" class="tab-panel"><section class="panel"><div class="actions" style="justify-content:space-between;margin-top:0"><h2 style="margin:0">Logs do processo</h2><form method="post" action="/clear-logs"><button class="subtle" type="submit">Limpar logs</button></form></div><p class="hint">Atualiza automaticamente. Mostra coleta, análise de currículo, triagem da IA, SMTP e Playwright.</p><div id="logs-body" class="log-console">{logs_view}</div></section></div></main>
<script>
(function () {{
  var inFlight = false;
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
  function refresh() {{
    if (inFlight || document.hidden) return;
    inFlight = true;
    fetch("/live", {{ headers: {{ Accept: "application/json" }} }})
      .then(function (response) {{ return response.ok ? response.json() : Promise.reject(); }})
      .then(function (data) {{
        var stateEl = document.getElementById("collector-state");
        var messageEl = document.getElementById("runtime-message");
        var statsEl = document.getElementById("job-stats");
        var jobsEl = document.getElementById("jobs-body");
        var historyEl = document.getElementById("run-history");
        var logsEl = document.getElementById("logs-body");
        var queueEl = document.getElementById("queue-body");
        if (stateEl && stateEl.textContent !== data.state_label) stateEl.textContent = data.state_label;
        if (messageEl && messageEl.textContent !== data.message) messageEl.textContent = data.message;
        if (statsEl && statsEl.innerHTML !== data.stats_html) statsEl.innerHTML = data.stats_html;
        if (jobsEl && jobsEl.innerHTML !== data.jobs_html) jobsEl.innerHTML = data.jobs_html;
        if (historyEl && historyEl.innerHTML !== data.history_html) historyEl.innerHTML = data.history_html;
        if (queueEl && data.queue_html && queueEl.innerHTML !== data.queue_html) queueEl.innerHTML = data.queue_html;
        if (logsEl && data.logs_html && logsEl.innerHTML !== data.logs_html) {{
          var stickBottom = logsEl.scrollTop + logsEl.clientHeight >= logsEl.scrollHeight - 40;
          logsEl.innerHTML = data.logs_html;
          if (stickBottom) logsEl.scrollTop = 0;
        }}
      }})
      .catch(function () {{}})
      .then(function () {{ inFlight = false; }});
  }}
  setInterval(refresh, 1500);
  document.addEventListener("visibilitychange", function () {{ if (!document.hidden) refresh(); }});
}})();
</script></body></html>'''


def parse_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
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

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/live":
            self.send_json(live_payload())
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
                with connect() as db:
                    message, kind, needs_analysis = persist_resume_upload(
                        db,
                        language=language,
                        original_filename=filename,
                        raw_bytes=raw,
                        resumes_dir=RESUMES_DIR,
                        now_iso=now_iso(),
                    )
                if needs_analysis:
                    qid = queue.enqueue(KIND_RESUME, {"language": language}, dedupe_key=f"resume:{language}")
                    message = f"{message} Job de fila #{qid}."
                log_event(kind if kind != "info" else "info", "resume", message)
                self.redirect(message, notice_kind=kind)
            except Exception as exc:
                log_event("error", "resume", f"Falha no upload: {exc}")
                self.redirect(f"Falha no upload do currículo: {exc}", notice_kind="error")
            return

        form = parse_form(self)
        if path == "/settings":
            save_settings(form)
            log_event("info", "settings", "Preferências de busca salvas.")
            self.redirect("Filtros salvos.")
        elif path == "/ai-settings":
            if "auto_apply" not in form:
                form["auto_apply"] = "0"
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
            log_event("info", "settings", f"Perfil/IA salvos (auto_apply={form.get('auto_apply')}).")
            self.redirect("Perfil e integrações salvos.")
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
            self.redirect("Logs limpos.", notice_kind="info")
        elif path == "/queue-cancel":
            jid = form.get("id", "")
            if jid.isdigit() and queue.cancel(int(jid)):
                self.redirect(f"Job #{jid} cancelado.", notice_kind="warning")
            else:
                self.redirect("Não foi possível cancelar o job.", notice_kind="error")
        elif path == "/queue-retry":
            jid = form.get("id", "")
            if jid.isdigit() and queue.retry_now(int(jid)):
                self.redirect(f"Job #{jid} reenfileirado.", notice_kind="info")
            else:
                self.redirect("Não foi possível reenfileirar o job.", notice_kind="error")
        elif path == "/queue-clear":
            n = queue.clear_terminal()
            self.redirect(f"{n} job(s) removido(s) da fila.", notice_kind="info")
        elif path == "/profile-settings":
            save_settings(form)
            with connect() as db:
                save_rules_from_form(db, form)
            log_event("info", "forms", "Regras de formulário salvas.")
            self.redirect("Regras de formulário salvas.")
        elif path == "/start":
            started = collector.start()
            self.redirect("Coleta iniciada." if started else "A coleta já está em execução.")
        elif path == "/stop":
            collector.stop()
            self.redirect("Solicitação para parar enviada.")
        elif path == "/job-status":
            if form.get("status") in STATUSES and form.get("id", "").isdigit():
                with connect() as db:
                    applied = now_iso() if form["status"] == "applied" else None
                    db.execute("UPDATE jobs SET status=?, applied_at=COALESCE(?, applied_at) WHERE id=?", (form["status"], applied, int(form["id"])))
            self.redirect("Etapa da vaga atualizada.")
        elif path == "/notes":
            if form.get("id", "").isdigit():
                with connect() as db:
                    db.execute("UPDATE jobs SET notes=? WHERE id=?", (form.get("notes", ""), int(form["id"])))
            self.redirect("Anotação salva.")
        else:
            self.send_page("Não encontrado", 404)

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    initialize()
    print(f"Radar de Vagas disponível em http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
