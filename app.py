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


ROOT = __file__.rsplit("\\", 1)[0]
DB_PATH = ROOT + "\\jobs.db"
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
        for key, value in DEFAULT_SETTINGS.items():
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))


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
Candidate-provided facts (the only source of claims about the candidate):
{facts or '[No candidate facts provided. Do not make claims about experience or skills.]'}

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
    except Exception as exc:
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


def create_cover_letter(job_id: int) -> None:
    cfg = settings()
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise ValueError("Vaga não encontrada.")
    language = job["language"]
    if language not in ("pt", "en"):
        raise ValueError("Idioma da vaga incerto. Corrija o idioma antes de gerar a carta.")
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    letter = ai_generate_letter(provider, model, language, dict(job), cfg)
    with connect() as db:
        db.execute("""INSERT INTO cover_letters(job_id,language,provider,model,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(job_id) DO UPDATE SET language=excluded.language,provider=excluded.provider,model=excluded.model,body=excluded.body,updated_at=excluded.updated_at""", (job_id, language, provider, model, letter,  now_iso(),  now_iso()))
        db.execute("UPDATE jobs SET status='prepared' WHERE id=? AND status='new'", (job_id,))


def ai_assess_job(job: dict, cfg: dict[str, str]) -> dict:
    """Classifica aderência e necessidade provável de carta antes da automação."""
    language = job.get("language", "unknown")
    if language not in ("pt", "en"):
        raise ValueError("Idioma incerto; a IA não vai escolher um currículo por suposição.")
    facts = cfg.get("candidate_facts_pt" if language == "pt" else "candidate_facts_en", "").strip()
    prompt = f"""Evaluate whether this candidate should apply to this job. Use only the candidate facts below; do not infer missing qualifications. Consider explicit location/work authorization, seniority, required skills, and role fit. Identify whether the job description asks for a cover letter. Return only JSON with keys: match_score (integer 0-100), should_apply (boolean), cover_letter_required (boolean), reason (short string in {('Portuguese' if language == 'pt' else 'English')}).
Candidate facts: {facts or '[none provided]'}
Job title: {job['title']}
Company: {job['company']}
Location/eligibility: {job['location']}
Description: {re.sub(r'<[^>]+>', ' ', job['description'])[:10000]}"""
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    api_key = get_ai_key(provider)
    if not api_key:
        raise ValueError("Configure a chave da API de IA para ativar o julgamento automático.")
    if provider == "openai":
        endpoint = "https://api.openai.com/v1/responses"
        payload = json.dumps({"model": model, "input": prompt, "text": {"format": {"type": "json_object"}}, "store": False, "max_output_tokens": 250}).encode("utf-8")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    elif provider == "gemini":
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
        payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 250, "responseMimeType": "application/json"}}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        raise ValueError("Provedor de IA inválido.")
    request = Request(endpoint, data=payload, headers=headers, method="POST")
    with urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    if provider == "openai":
        raw = "\n".join(part.get("text", "") for item in result.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text")
    else:
        raw = "\n".join(part.get("text", "") for item in result.get("candidates", []) for part in item.get("content", {}).get("parts", []))
    decision = json.loads(raw.strip())
    return {"match_score": max(0, min(100, int(decision.get("match_score", 0)))), "should_apply": bool(decision.get("should_apply")), "letter_required": bool(decision.get("cover_letter_required")), "reason": str(decision.get("reason", ""))[:1000], "provider": provider, "model": model}


# Os feeds ativos só localizam vagas. Este registro ficará vazio até existir um endpoint
# oficial de candidatura acessível ao candidato, com requisitos e autorização documentados.
APPLICATION_PROVIDERS: dict[str, object] = {}


def process_auto_job(job_id: int) -> str:
    """Faz triagem automática, prepara a carta quando indicada e registra limites da fonte."""
    cfg = settings()
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return "vaga removida"
    decision = ai_assess_job(dict(job), cfg)
    minimum = max(0, min(100, int(cfg.get("minimum_match_score", "80"))))
    qualifies = decision["should_apply"] and decision["match_score"] >= minimum
    with connect() as db:
        db.execute("INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason) VALUES(?,?,?,?,?,?)", (job_id,  now_iso(), decision["match_score"], int(qualifies), int(decision["letter_required"]), decision["reason"]))
    if not qualifies:
        with connect() as db:
            db.execute("UPDATE jobs SET status='ignored',notes=? WHERE id=?", (f"IA: score {decision['match_score']}/100. {decision['reason']}", job_id))
        return f"IA descartou vaga {job_id} ({decision['match_score']}/100)"
    if decision["letter_required"]:
        create_cover_letter(job_id)
    applier = APPLICATION_PROVIDERS.get(job["source"].casefold())
    if not applier:
        with connect() as db:
            db.execute("UPDATE jobs SET status='blocked',notes=? WHERE id=?", (f"IA recomendou candidatura ({decision['match_score']}/100), mas {job['source']} não fornece conector oficial de envio configurado. {decision['reason']}", job_id))
        return f"vaga {job_id}: envio automático indisponível em {job['source']}"
    # Appliers are implemented per platform; never guess application fields or bypass a platform.
    return str(applier(dict(job), cfg, decision))


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

    def _run_once(self) -> None:
        started =  now_iso()
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
            try:
                jobs = provider()
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
        finished =  now_iso()
        message = f"Busca concluída: {found} vaga(s) nova(s)." + (" Avisos: " + "; ".join(errors) if errors else "")
        with connect() as db:
            db.execute("UPDATE runs SET finished_at=?,state=?,found_count=?,message=? WHERE id=?", (finished, "completed" if not self.stop_event.is_set() else "stopped", found, message, run_id))
        with self.lock:
            self.last_run, self.last_found, self.message = finished, found, message


collector = Collector()


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
        letter_ui = f'<details><summary>{"Carta criada" if job["cover_letter"] else "Carta não criada"}</summary><div class="description">{esc(job["cover_letter"] or "A carta será gerada automaticamente quando uma candidatura compatível solicitar esse documento.")}</div></details>'
        rows.append(f'''<tr><td><a class="job-title" href="{esc(job['url'])}" target="_blank" rel="noreferrer">{esc(job['title'])}</a><small>{esc(job['company'])}</small></td><td>{esc(job['location'])}</td><td><span class="source">{esc(job['source'])}</span></td><td><span title="Confiança do detector: {confidence}">{esc(lang_label + confidence)}</span></td><td>{esc(STATUSES.get(job['status'], job['status']))}</td><td>{letter_ui}<details><summary>Descrição</summary><div class="description">{esc(description[:1800])}</div></details></td><td><small>{esc(format_brasilia(job['posted_at'] or job['first_seen_at']))}</small></td></tr>''')
    if rows:
        return "".join(rows)
    if collecting:
        return '<tr><td colspan="7">Buscando vagas… as novas aparecem aqui assim que forem encontradas.</td></tr>'
    return '<tr><td colspan="7">Nenhuma vaga ainda. Configure as fontes e inicie o bot.</td></tr>'


def history_html(runs) -> str:
    return "".join(f'<li><span>{esc(run["started_at"][:16].replace("T", " "))}</span> {esc(run["message"] or run["state"])}</li>' for run in runs) or "<li>Nenhuma execução ainda.</li>"


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
    }


def render_page(notice: str = "") -> str:
    """Monta o painel local: preferências, controles, histórico e vagas capturadas."""
    cfg = settings()
    status = collector.snapshot()
    counts, jobs, runs = load_dashboard()
    collecting = status["state"] in {"running", "stopping"}
    cards = stats_html(counts)
    rows = job_rows_html(jobs, collecting)
    history = history_html(runs)
    state_label = state_label_for(status["state"])
    return f'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar de Vagas</title><style>
      :root{{--ink:#172b36;--muted:#62747d;--line:#dce5e8;--paper:#f4f7f7;--teal:#0b786d;--mint:#d8f0e9;--white:#fff}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 Inter,Segoe UI,Arial,sans-serif}}header{{background:#102d35;color:white;padding:28px max(24px,calc((100vw - 1280px)/2));display:flex;justify-content:space-between;align-items:center}}h1{{font-size:25px;margin:0}}header p{{margin:5px 0 0;color:#c1d4d6}}main{{max-width:1280px;margin:26px auto;padding:0 24px}}.top{{display:grid;grid-template-columns:1.3fr .7fr;gap:18px}}.panel,.stat,.table-wrap{{background:white;border:1px solid var(--line);border-radius:13px}}.panel{{padding:20px}}h2{{font-size:18px;margin:0 0 14px}}.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}label{{display:block;color:var(--muted);font-size:13px;font-weight:600}}input,textarea,select{{font:inherit;color:var(--ink);width:100%;margin-top:5px;padding:9px 10px;border:1px solid #cdd9dc;border-radius:8px;background:white}}.hint{{color:var(--muted);font-size:12px;margin:10px 0}}button{{border:0;border-radius:8px;padding:10px 15px;background:var(--teal);color:white;font-weight:650;cursor:pointer}}button.stop{{background:#a74639}}button.subtle{{padding:7px 10px;background:#eaf2f1;color:var(--ink);margin-top:6px}}.actions{{display:flex;gap:9px;margin-top:12px;align-items:center}}.runtime{{color:var(--muted);font-size:13px}}.stats{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:18px 0}}.stat{{padding:13px 15px}}.stat span{{display:block;font-size:12px;color:var(--muted)}}.stat strong{{font-size:23px}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:950px}}th,td{{padding:13px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{font-size:12px;color:var(--muted);background:#f8fafa}}td small{{display:block;color:var(--muted);margin-top:3px}}.job-title{{font-weight:700;color:#145d59;text-decoration:none}}.job-title:hover{{text-decoration:underline}}.source{{background:var(--mint);padding:3px 7px;border-radius:99px;font-size:12px}}select{{min-width:150px;margin:0;padding:7px}}summary{{cursor:pointer;color:var(--teal);font-size:13px}}.description{{max-width:350px;max-height:220px;overflow:auto;padding:8px 0;font-size:13px}}details textarea{{min-width:230px}}.history{{color:var(--muted);font-size:13px;padding-left:20px}}.notice{{padding:10px 13px;background:#e7f4ed;border-radius:8px;margin-bottom:15px}}@media(max-width:800px){{.top{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}header{{padding:20px 24px}}.form-grid{{grid-template-columns:1fr}}}}
      </style></head><body><header><div><h1>Radar de Vagas</h1><p>Busca, seleção e candidaturas automáticas</p></div><span id="collector-state">{esc(state_label)}</span></header><main>{f'<div class="notice">{esc(notice)}</div>' if notice else ''}<div class="top"><section class="panel"><h2>Preferências de busca</h2><form method="post" action="/settings"><div class="form-grid"><label>Cargos e termos, separados por vírgula<textarea name="keywords" rows="3">{esc(cfg.get('keywords',''))}</textarea></label><label>Países/regiões aceitos<textarea name="locations" rows="3">{esc(cfg.get('locations',''))}</textarea></label><label>Fontes: remotive, remoteok, adzuna, apify<input name="sources" value="{esc(cfg.get('sources',''))}"></label><label>Intervalo de busca (minutos)<input name="interval_minutes" type="number" min="5" value="{esc(cfg.get('interval_minutes','15'))}"></label><label>Países Adzuna (ex.: br,us,gb,ca)<input name="adzuna_countries" value="{esc(cfg.get('adzuna_countries','br,us,gb,ca'))}"></label><label>Limite mensal Apify (USD)<input name="apify_monthly_credit_limit_usd" type="number" min="0" step="0.01" value="{esc(cfg.get('apify_monthly_credit_limit_usd','5'))}"></label><label>Máximo de vagas por ciclo Apify<input name="apify_job_count" type="number" min="1" max="100" value="{esc(cfg.get('apify_job_count','25'))}"></label></div><button>Salvar preferências</button></form><div class="actions"><form method="post" action="/start"><button>Iniciar bot</button></form><form method="post" action="/stop"><button class="stop">Parar bot</button></form><span class="runtime" id="runtime-message">{esc(status['message'])}</span></div></section><section class="panel"><h2>Execuções recentes</h2><ul class="history" id="run-history">{history}</ul></section></div><section class="panel" style="margin-top:18px"><h2>Perfil e automação</h2><form method="post" action="/ai-settings"><div class="form-grid"><label>Provedor de IA<select name="ai_provider"><option value="gemini" {'selected' if cfg.get('ai_provider') == 'gemini' else ''}>Gemini</option><option value="openai" {'selected' if cfg.get('ai_provider') == 'openai' else ''}>OpenAI</option></select></label><label>Modelo<input name="ai_model" value="{esc(cfg.get('ai_model','gemini-2.5-flash'))}"></label><label>Seu nome<input name="candidate_name" value="{esc(cfg.get('candidate_name',''))}"></label><label>Chave de IA (vazio mantém a salva)<input type="password" name="api_key" autocomplete="new-password"></label><label>Fatos profissionais em português<textarea name="candidate_facts_pt" rows="4">{esc(cfg.get('candidate_facts_pt',''))}</textarea></label><label>Professional facts in English<textarea name="candidate_facts_en" rows="4">{esc(cfg.get('candidate_facts_en',''))}</textarea></label><label>Currículo em português (caminho local)<input name="resume_pt_path" value="{esc(cfg.get('resume_pt_path',''))}"></label><label>English résumé (local path)<input name="resume_en_path" value="{esc(cfg.get('resume_en_path',''))}"></label><label>Score mínimo escolhido pela IA (%)<input name="minimum_match_score" type="number" min="0" max="100" value="{esc(cfg.get('minimum_match_score','80'))}"></label><label>Máximo de candidaturas por ciclo<input name="maximum_applications_per_run" type="number" min="1" max="50" value="{esc(cfg.get('maximum_applications_per_run','5'))}"></label><label>Adzuna App ID (vazio mantém o salvo)<input name="adzuna_app_id" value=""></label><label>Adzuna API key (vazio mantém a salva)<input type="password" name="adzuna_app_key" value=""></label><label>Token Apify (vazio mantém o salvo)<input type="password" name="apify_token" value="" autocomplete="new-password"></label></div><label style="margin:12px 0"><input type="checkbox" name="auto_apply" value="1" {'checked' if cfg.get('auto_apply') == '1' else ''} style="width:auto"> Ativar seleção e candidatura automáticas onde a fonte permitir</label><p class="hint">A IA cria somente cartas de apresentação. Não cria nem altera currículos; os arquivos ficam no caminho local informado. Chaves ficam no cofre de credenciais do Windows. O Adzuna permite pesquisa pessoal pela API e limita o uso a 25 chamadas/minuto, 250/dia, 1.000/semana e 2.500/mês; mantenha a atribuição exibida nos resultados. A fonte apify usa o <a href="https://apify.com/api/job-scraping-api">job scraping API da Apify</a> em paralelo às demais; o bot para de chamá-la quando o uso do ciclo mensal da conta atingir o limite em USD. Token: console Apify → API &amp; Integrations.{f" Último uso consultado: {esc(cfg.get('apify_last_usage_usd','?'))} USD ({esc(cfg.get('apify_last_usage_cycle','ciclo atual'))})." if cfg.get('apify_last_usage_usd') else ""} O envio automático requer um endpoint de candidatura autorizado pela fonte.</p><button>Salvar perfil e automação</button></form></section><div class="stats" id="job-stats">{cards}</div><section class="table-wrap"><table><thead><tr><th>Vaga</th><th>Localidade</th><th>Fonte</th><th>Idioma</th><th>Etapa</th><th>Carta e descrição</th><th>Data</th></tr></thead><tbody id="jobs-body">{rows}</tbody></table></section></main>
<script>
(function () {{
  var inFlight = false;
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
        if (stateEl && stateEl.textContent !== data.state_label) stateEl.textContent = data.state_label;
        if (messageEl && messageEl.textContent !== data.message) messageEl.textContent = data.message;
        if (statsEl && statsEl.innerHTML !== data.stats_html) statsEl.innerHTML = data.stats_html;
        if (jobsEl && jobsEl.innerHTML !== data.jobs_html) jobsEl.innerHTML = data.jobs_html;
        if (historyEl && historyEl.innerHTML !== data.history_html) historyEl.innerHTML = data.history_html;
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

    def redirect(self, notice: str = "") -> None:
        body = render_page(notice)
        self.send_page(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/live":
            self.send_json(live_payload())
            return
        self.send_page(render_page())

    def do_POST(self) -> None:
        form = parse_form(self)
        if self.path == "/settings":
            save_settings(form)
            self.redirect("Filtros salvos.")
        elif self.path == "/ai-settings":
            save_settings(form)
            provider = form.get("ai_provider", "gemini").casefold()
            try:
                save_ai_key(provider, form.get("api_key", ""))
                secret_set("adzuna_app_id", form.get("adzuna_app_id", ""))
                secret_set("adzuna_app_key", form.get("adzuna_app_key", ""))
                secret_set("apify_token", form.get("apify_token", ""))
            except RuntimeError as exc:
                self.redirect(str(exc))
                return
            self.redirect("Perfil e integrações salvos.")
        elif self.path == "/start":
            started = collector.start()
            self.redirect("Coleta iniciada." if started else "A coleta já está em execução.")
        elif self.path == "/stop":
            collector.stop()
            self.redirect("Solicitação para parar enviada.")
        elif self.path == "/job-status":
            if form.get("status") in STATUSES and form.get("id", "").isdigit():
                with connect() as db:
                    applied =  now_iso() if form["status"] == "applied" else None
                    db.execute("UPDATE jobs SET status=?, applied_at=COALESCE(?, applied_at) WHERE id=?", (form["status"], applied, int(form["id"])))
            self.redirect("Etapa da vaga atualizada.")
        elif self.path == "/notes":
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
