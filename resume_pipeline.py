# Upload, extração e análise única de currículos PDF.
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen


class AiUnavailableError(RuntimeError):
    """Chave ausente, quota esgotada ou falha de autenticação/billing da IA."""


def extract_pdf_text(path: str) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    chunks: list[str] = []
    for page in reader.pages:
        chunks.append(page.extract_text() or "")
    text = re.sub(r"\s+", " ", "\n".join(chunks)).strip()
    if len(text) < 40:
        raise ValueError("Não foi possível extrair texto útil deste PDF. Use um PDF com texto selecionável.")
    return text[:50000]


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_resume(db: sqlite3.Connection, language: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM resumes WHERE language=?", (language,)).fetchone()


def resume_summaries(db: sqlite3.Connection) -> dict[str, str]:
    rows = db.execute("SELECT language, analysis_summary FROM resumes").fetchall()
    return {row["language"]: row["analysis_summary"] or "" for row in rows}


def analyze_resume_text(
    text: str,
    language: str,
    *,
    provider: str,
    model: str,
    api_key: str,
    call_json: Callable[..., dict] | None = None,
) -> dict:
    language_name = "Portuguese" if language == "pt" else "English"
    prompt = f"""Analyze this resume written primarily in {language_name}. Return ONLY JSON with keys:
summary (string, concise professional summary in {language_name}),
skills (array of strings),
experience (array of short strings: role, employer, period, highlights — only what is stated),
education (array of strings),
seniority (string),
languages (array of strings),
work_authorization_notes (string, only if stated),
location_notes (string, only if stated).
Never invent employers, dates, skills, or results. If unknown, use empty string or empty array.

Resume text:
{text[:20000]}
"""
    if call_json is not None:
        return call_json(provider, model, api_key, prompt)

    if not api_key:
        raise AiUnavailableError("Configure a chave da API de IA antes de analisar o currículo.")
    provider = provider.casefold().strip()
    try:
        if provider == "openai":
            endpoint = "https://api.openai.com/v1/responses"
            payload = json.dumps(
                {"model": model, "input": prompt, "text": {"format": {"type": "json_object"}}, "store": False, "max_output_tokens": 1200}
            ).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == "gemini":
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
            payload = json.dumps(
                {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 1200, "responseMimeType": "application/json"}}
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            raise ValueError("Provedor de IA inválido.")
        request = Request(endpoint, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        if exc.code in {401, 403, 429} or "quota" in body.casefold() or "billing" in body.casefold():
            raise AiUnavailableError(f"IA indisponível (HTTP {exc.code}).") from exc
        raise RuntimeError(f"Falha ao analisar currículo (HTTP {exc.code}): {body}") from exc
    except AiUnavailableError:
        raise
    except Exception as exc:
        message = str(exc).casefold()
        if "quota" in message or "insufficient" in message or "api key" in message:
            raise AiUnavailableError(f"IA indisponível: {exc}") from exc
        raise

    if provider == "openai":
        raw = "\n".join(part.get("text", "") for item in result.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text")
    else:
        raw = "\n".join(part.get("text", "") for item in result.get("candidates", []) for part in item.get("content", {}).get("parts", []))
    data = json.loads(raw.strip())
    if not isinstance(data, dict):
        raise RuntimeError("A análise do currículo não retornou JSON objeto.")
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise RuntimeError("A análise do currículo não retornou um resumo.")
    return data


def _upsert_resume(
    db: sqlite3.Connection,
    *,
    language: str,
    original_filename: str,
    stored_path: str,
    digest: str,
    extracted_text: str,
    analysis_json: str,
    analysis_summary: str,
    analyzed_at: str | None,
    provider: str,
    model: str,
    analysis_status: str,
    analysis_error: str,
    analysis_message: str,
    now_iso: str,
) -> None:
    db.execute(
        """INSERT INTO resumes(
             language,original_filename,stored_path,file_sha256,extracted_text,analysis_json,analysis_summary,
             analyzed_at,provider,model,analysis_status,analysis_error,analysis_message,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(language) DO UPDATE SET
             original_filename=excluded.original_filename,
             stored_path=excluded.stored_path,
             file_sha256=excluded.file_sha256,
             extracted_text=excluded.extracted_text,
             analysis_json=excluded.analysis_json,
             analysis_summary=excluded.analysis_summary,
             analyzed_at=excluded.analyzed_at,
             provider=excluded.provider,
             model=excluded.model,
             analysis_status=excluded.analysis_status,
             analysis_error=excluded.analysis_error,
             analysis_message=excluded.analysis_message,
             updated_at=excluded.updated_at
        """,
        (
            language,
            original_filename,
            stored_path,
            digest,
            extracted_text,
            analysis_json,
            analysis_summary,
            analyzed_at,
            provider,
            model,
            analysis_status,
            analysis_error[:1000],
            analysis_message[:500],
            now_iso,
            now_iso,
        ),
    )


def persist_resume_upload(
    db: sqlite3.Connection,
    *,
    language: str,
    original_filename: str,
    raw_bytes: bytes,
    resumes_dir: str,
    now_iso: str,
) -> tuple[str, str, bool]:
    """Salva o PDF e extrai texto. Não chama a IA.

    Retorna (mensagem, notice_kind, needs_analysis).
    """
    if language not in {"pt", "en"}:
        raise ValueError("Idioma do currículo deve ser pt ou en.")
    if not raw_bytes.startswith(b"%PDF"):
        raise ValueError("Envie um arquivo PDF válido.")
    if len(raw_bytes) > 12 * 1024 * 1024:
        raise ValueError("PDF maior que 12 MB.")

    os.makedirs(resumes_dir, exist_ok=True)
    stored_name = f"resume_{language}.pdf"
    stored_path = os.path.join(resumes_dir, stored_name)
    temp_path = stored_path + ".tmp"
    with open(temp_path, "wb") as handle:
        handle.write(raw_bytes)
    digest = file_sha256(temp_path)
    existing = get_resume(db, language)
    if existing and existing["file_sha256"] == digest and (existing["analysis_summary"] or "").strip():
        os.replace(temp_path, stored_path)
        message = f"Currículo {language.upper()}: PDF idêntico ao já analisado. Análise da IA reutilizada (sem nova chamada)."
        db.execute(
            """UPDATE resumes SET original_filename=?, stored_path=?, analysis_status='reused',
                   analysis_error='', analysis_message=?, updated_at=? WHERE language=?""",
            (original_filename, stored_path, message, now_iso, language),
        )
        return message, "info", False

    extracted = extract_pdf_text(temp_path)
    os.replace(temp_path, stored_path)
    message = (
        f"Currículo {language.upper()}: PDF salvo. Análise da IA enfileirada "
        "(será retentada automaticamente se a API estiver ocupada)."
    )
    _upsert_resume(
        db,
        language=language,
        original_filename=original_filename,
        stored_path=stored_path,
        digest=digest,
        extracted_text=extracted,
        analysis_json=(existing["analysis_json"] if existing else "") or "",
        analysis_summary=(existing["analysis_summary"] if existing else "") or "",
        analyzed_at=existing["analyzed_at"] if existing else None,
        provider="",
        model="",
        analysis_status="pending",
        analysis_error="",
        analysis_message=message,
        now_iso=now_iso,
    )
    return message, "info", True


def run_resume_analysis(
    db: sqlite3.Connection,
    *,
    language: str,
    provider: str,
    model: str,
    api_key: str,
    now_iso: str,
) -> str:
    """Executa a análise de IA do currículo já persistido. Pode levantar AiUnavailableError."""
    row = get_resume(db, language)
    if not row:
        raise ValueError(f"Nenhum currículo {language} salvo para analisar.")
    text = (row["extracted_text"] or "").strip()
    if not text:
        if row["stored_path"] and os.path.exists(row["stored_path"]):
            text = extract_pdf_text(row["stored_path"])
        else:
            raise ValueError("Currículo sem texto extraído.")
    analysis = analyze_resume_text(text, language, provider=provider, model=model, api_key=api_key)
    summary = str(analysis.get("summary") or "").strip()
    message = f"Currículo {language.upper()}: análise da IA concluída ({provider}/{model})."
    _upsert_resume(
        db,
        language=language,
        original_filename=row["original_filename"],
        stored_path=row["stored_path"],
        digest=row["file_sha256"],
        extracted_text=text,
        analysis_json=json.dumps(analysis, ensure_ascii=False),
        analysis_summary=summary,
        analyzed_at=now_iso,
        provider=provider,
        model=model,
        analysis_status="ok",
        analysis_error="",
        analysis_message=message,
        now_iso=now_iso,
    )
    return message


def mark_resume_analysis_error(db: sqlite3.Connection, language: str, error: str, now_iso: str) -> None:
    row = get_resume(db, language)
    if not row:
        return
    db.execute(
        """UPDATE resumes SET analysis_status='error', analysis_error=?, analysis_message=?, updated_at=?
           WHERE language=?""",
        (error[:1000], f"Currículo {language.upper()}: falha na análise — {error}"[:500], now_iso, language),
    )


# Compat: antigo nome usado em imports/testes.
def store_resume_upload(*args, **kwargs):  # type: ignore[no-untyped-def]
    raise RuntimeError("Use persist_resume_upload + fila de análise (run_resume_analysis).")
