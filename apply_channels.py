# Canais de candidatura: e-mail (SMTP) e formulário público (Playwright).
from __future__ import annotations

import json
import logging
import re
import smtplib
import sqlite3
from email.message import EmailMessage
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from form_rules import find_rule_for_label, list_rules, pick_select_option, resolve_rule_value
from resume_pipeline import AiUnavailableError, get_resume

LOG = logging.getLogger("job-scraper")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
SKIP_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "notifications"}
OPEN_QUESTION_HINTS = (
    "describe", "tell us", "why do you", "why are you", "por que", "porque", "descreva",
    "conte", "fale sobre", "project", "projeto", "experience with", "experiencia com",
    "what makes", "additional", "essay", "cover your",
)


def extract_emails(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in EMAIL_RE.findall(text or ""):
        local = match.split("@", 1)[0].casefold()
        if any(skip in local for skip in SKIP_LOCAL):
            continue
        key = match.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(match)
    return found


def send_smtp_email(
    *,
    cfg: dict[str, str],
    password: str,
    to_addrs: list[str],
    subject: str,
    body: str,
    attachment_path: str | None,
    attachment_name: str | None = None,
) -> None:
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        raise ValueError("Configure o host SMTP no painel.")
    try:
        port = int(cfg.get("smtp_port") or "587")
    except ValueError as exc:
        raise ValueError("Porta SMTP inválida.") from exc
    user = (cfg.get("smtp_user") or "").strip()
    from_addr = (cfg.get("smtp_from") or cfg.get("candidate_email") or user).strip()
    if not from_addr:
        raise ValueError("Configure o e-mail remetente (SMTP from / candidate_email).")
    if not password and user:
        raise ValueError("Configure a senha SMTP no painel.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = ", ".join(to_addrs)
    message.set_content(body)
    if attachment_path:
        with open(attachment_path, "rb") as handle:
            data = handle.read()
        name = attachment_name or attachment_path.rsplit("/", 1)[-1]
        message.add_attachment(data, maintype="application", subtype="pdf", filename=name)

    use_tls = (cfg.get("smtp_use_tls") or "1") == "1"
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=45) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=45) as smtp:
            smtp.ehlo()
            if use_tls:
                smtp.starttls()
                smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)


def lookslike_open_question(label: str, tag: str = "textarea") -> bool:
    hay = (label or "").casefold()
    if tag == "textarea" and len(hay) > 12:
        return True
    return any(hint in hay for hint in OPEN_QUESTION_HINTS)


def generate_open_answer(
    *,
    question: str,
    job: dict,
    resume_summary: str,
    resume_json: str,
    facts: str,
    provider: str,
    model: str,
    api_key: str,
) -> str:
    if not api_key:
        raise AiUnavailableError("IA indisponível: chave não configurada.")
    language = job.get("language") or "en"
    language_name = "Portuguese" if language == "pt" else "English"
    prompt = f"""Answer this job-application screening question in {language_name}.
Use ONLY the resume analysis and candidate facts. Never invent projects, employers, metrics, or skills.
If the facts are insufficient, reply with exactly: INSUFFICIENT_FACTS
Return only the answer text.

Question: {question}
Candidate facts: {facts or '[none]'}
Resume summary: {resume_summary or '[none]'}
Resume analysis JSON: {resume_json[:8000] or '[none]'}
Job title: {job.get('title')}
Company: {job.get('company')}
"""
    provider = provider.casefold().strip()
    try:
        if provider == "openai":
            endpoint = "https://api.openai.com/v1/responses"
            payload = json.dumps({"model": model, "input": prompt, "store": False, "max_output_tokens": 450}).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == "gemini":
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
            payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 450, "temperature": 0.3}}).encode("utf-8")
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
    except Exception as exc:
        msg = str(exc).casefold()
        if "quota" in msg or "api key" in msg or "401" in msg or "429" in msg:
            raise AiUnavailableError(str(exc)) from exc
        raise

    if provider == "openai":
        answer = "\n".join(part.get("text", "") for item in result.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text").strip()
    else:
        answer = "\n".join(part.get("text", "") for item in result.get("candidates", []) for part in item.get("content", {}).get("parts", [])).strip()
    if not answer or answer.strip() == "INSUFFICIENT_FACTS":
        raise ValueError("IA sem base suficiente para responder a pergunta aberta.")
    return answer[:1200]


def apply_via_email(
    db: sqlite3.Connection,
    job: dict,
    cfg: dict[str, str],
    *,
    cover_letter: str,
    resume_path: str,
    resume_id: int,
    cover_letter_id: int | None,
    smtp_password: str,
    now_iso: str,
) -> tuple[bool, str]:
    blob = " ".join((job.get("description") or "", job.get("url") or "", job.get("company") or ""))
    recipients = extract_emails(blob)
    if not recipients:
        return False, "Nenhum e-mail de contato encontrado na vaga."
    if not (cfg.get("smtp_host") or "").strip():
        return False, "SMTP não configurado."
    subject = f"Candidatura: {job.get('title', '')} — {cfg.get('candidate_name') or 'candidato'}"
    try:
        send_smtp_email(
            cfg=cfg,
            password=smtp_password,
            to_addrs=recipients[:3],
            subject=subject,
            body=cover_letter or "(carta não gerada)",
            attachment_path=resume_path,
            attachment_name=resume_path.rsplit("/", 1)[-1],
        )
    except Exception as exc:
        db.execute(
            """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (job["id"], "email", ",".join(recipients[:3]), resume_id, cover_letter_id, "failed", str(exc)[:500], now_iso),
        )
        return False, f"Falha SMTP: {exc}"
    db.execute(
        """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (job["id"], "email", ",".join(recipients[:3]), resume_id, cover_letter_id, "sent", "E-mail enviado", now_iso),
    )
    return True, f"E-mail enviado para {', '.join(recipients[:3])}"


def apply_via_browser(
    db: sqlite3.Connection,
    job: dict,
    cfg: dict[str, str],
    *,
    cover_letter: str,
    resume_path: str,
    resume_id: int,
    cover_letter_id: int | None,
    resume_summary: str,
    resume_json: str,
    provider: str,
    model: str,
    api_key: str,
    now_iso: str,
) -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "Playwright não instalado. Rode: pip install playwright && playwright install chromium"

    rules = list_rules(db)
    facts = cfg.get("candidate_facts_pt" if job.get("language") == "pt" else "candidate_facts_en", "")
    open_count = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            content = page.content().casefold()
            if any(token in content for token in ("sign in", "log in", "fazer login", "captcha", "cf-challenge")):
                browser.close()
                detail = "Página exige login ou CAPTCHA; envio automático pulado."
                db.execute(
                    """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (job["id"], "browser", "", resume_id, cover_letter_id, "blocked", detail, now_iso),
                )
                return False, detail

            # Collect labeled controls
            controls = page.evaluate(
                """() => {
                  const out = [];
                  const nodes = document.querySelectorAll('input, textarea, select');
                  for (const el of nodes) {
                    if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button' || el.disabled) continue;
                    const id = el.id || '';
                    let label = '';
                    if (id) {
                      const lab = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                      if (lab) label = lab.innerText || '';
                    }
                    if (!label && el.closest('label')) label = el.closest('label').innerText || '';
                    if (!label) label = [el.name, el.placeholder, el.getAttribute('aria-label'), el.id].filter(Boolean).join(' ');
                    const options = el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text) : [];
                    out.push({ tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), name: el.name || '', label, options });
                  }
                  return out;
                }"""
            )
            if not controls:
                browser.close()
                detail = "Nenhum formulário público detectado."
                db.execute(
                    """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (job["id"], "browser", "", resume_id, cover_letter_id, "blocked", detail, now_iso),
                )
                return False, detail

            for control in controls:
                label = control.get("label") or control.get("name") or ""
                tag = control.get("tag")
                ctype = control.get("type")
                rule = find_rule_for_label(label, rules)
                selector_bits = []
                if control.get("name"):
                    selector_bits.append(f'[name="{control["name"]}"]')
                locator = page.locator(",".join(selector_bits)).first if selector_bits else None

                if rule and str(rule["mode"]) == "skip":
                    continue

                if rule is None and lookslike_open_question(label, tag if tag == "textarea" else "input"):
                    open_count += 1
                    if open_count > 5:
                        browser.close()
                        detail = "Muitas perguntas abertas; vaga pulada."
                        db.execute(
                            """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (job["id"], "browser", "", resume_id, cover_letter_id, "blocked", detail, now_iso),
                        )
                        return False, detail
                    try:
                        answer = generate_open_answer(
                            question=label,
                            job=job,
                            resume_summary=resume_summary,
                            resume_json=resume_json,
                            facts=facts,
                            provider=provider,
                            model=model,
                            api_key=api_key,
                        )
                    except AiUnavailableError as exc:
                        browser.close()
                        detail = f"Pergunta aberta detectada; IA indisponível — vaga pulada. {exc}"
                        db.execute(
                            """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (job["id"], "browser", "", resume_id, cover_letter_id, "blocked", detail[:500], now_iso),
                        )
                        return False, detail
                    except Exception as exc:
                        browser.close()
                        detail = f"Não foi possível responder pergunta aberta; vaga pulada. {exc}"
                        db.execute(
                            """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (job["id"], "browser", "", resume_id, cover_letter_id, "blocked", detail[:500], now_iso),
                        )
                        return False, detail
                    db.execute(
                        """INSERT INTO form_answers(job_id,question,answer,provider,model,created_at) VALUES(?,?,?,?,?,?)""",
                        (job["id"], label[:500], answer, provider, model, now_iso),
                    )
                    if locator:
                        locator.fill(answer)
                    continue

                if rule is None:
                    # required unknown field: skip optional anonymous inputs
                    continue

                value = resolve_rule_value(rule, cfg, cover_letter=cover_letter, resume_path=resume_path)
                if value is None or value == "":
                    if str(rule["mode"]) in {"select", "file"} or (tag == "textarea"):
                        browser.close()
                        detail = f"Campo obrigatório sem valor configurado: {rule['key']}"
                        db.execute(
                            """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (job["id"], "browser", "", resume_id, cover_letter_id, "blocked", detail, now_iso),
                        )
                        return False, detail
                    continue

                if not locator:
                    continue
                mode = str(rule["mode"])
                if mode == "file" or ctype == "file":
                    locator.set_input_files(resume_path)
                elif tag == "select" or mode == "select":
                    options = control.get("options") or []
                    chosen = pick_select_option(list(options), value)
                    if not chosen:
                        browser.close()
                        detail = f"Nenhuma opção de select compatível para {rule['key']} (valor: {value})"
                        db.execute(
                            """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (job["id"], "browser", "", resume_id, cover_letter_id, "blocked", detail, now_iso),
                        )
                        return False, detail
                    locator.select_option(label=chosen)
                else:
                    locator.fill(value)

            # Try submit
            submitted = False
            for text in ("Submit", "Apply", "Enviar", "Candidatar", "Send application", "Apply now"):
                btn = page.get_by_role("button", name=re.compile(text, re.I))
                if btn.count():
                    btn.first.click(timeout=5000)
                    submitted = True
                    break
            if not submitted:
                page.locator('input[type="submit"]').first.click(timeout=3000)
                submitted = True
            page.wait_for_timeout(1500)
            browser.close()
        except Exception as exc:
            try:
                browser.close()
            except Exception:
                pass
            detail = f"Falha no Playwright: {exc}"
            db.execute(
                """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (job["id"], "browser", "", resume_id, cover_letter_id, "failed", detail[:500], now_iso),
            )
            return False, detail

    db.execute(
        """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (job["id"], "browser", "", resume_id, cover_letter_id, "sent", "Formulário enviado via Playwright", now_iso),
    )
    return True, "Formulário enviado via Playwright"


def record_blocked(db: sqlite3.Connection, job_id: int, detail: str, now_iso: str, resume_id: int | None = None, cover_letter_id: int | None = None) -> None:
    db.execute(
        """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (job_id, "none", "", resume_id, cover_letter_id, "blocked", detail[:500], now_iso),
    )
