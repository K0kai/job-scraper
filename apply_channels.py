# Canal de candidatura automática: somente e-mail (SMTP).
from __future__ import annotations

import re
import smtplib
import sqlite3
from email.message import EmailMessage
from typing import Callable


EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
SKIP_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "notifications"}
ConnectFn = Callable[[], sqlite3.Connection]


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


def _insert_application(
    connect_fn: ConnectFn,
    *,
    job_id: int,
    channel: str,
    recipient: str,
    resume_id: int | None,
    cover_letter_id: int | None,
    status: str,
    detail: str,
    now_iso: str,
) -> None:
    with connect_fn() as db:
        db.execute(
            """INSERT INTO applications(job_id,channel,recipient,resume_id,cover_letter_id,status,detail,attempted_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (job_id, channel, recipient, resume_id, cover_letter_id, status, detail[:500], now_iso),
        )


def apply_via_email(
    connect_fn: ConnectFn,
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
    recipient_csv = ",".join(recipients[:3])
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
        _insert_application(
            connect_fn,
            job_id=job["id"],
            channel="email",
            recipient=recipient_csv,
            resume_id=resume_id,
            cover_letter_id=cover_letter_id,
            status="failed",
            detail=str(exc)[:500],
            now_iso=now_iso,
        )
        return False, f"Falha SMTP: {exc}"
    _insert_application(
        connect_fn,
        job_id=job["id"],
        channel="email",
        recipient=recipient_csv,
        resume_id=resume_id,
        cover_letter_id=cover_letter_id,
        status="sent",
        detail="E-mail enviado",
        now_iso=now_iso,
    )
    return True, f"E-mail enviado para {recipient_csv}"


def record_blocked(
    connect_fn: ConnectFn,
    job_id: int,
    detail: str,
    now_iso: str,
    resume_id: int | None = None,
    cover_letter_id: int | None = None,
) -> None:
    _insert_application(
        connect_fn,
        job_id=job_id,
        channel="none",
        recipient="",
        resume_id=resume_id,
        cover_letter_id=cover_letter_id,
        status="blocked",
        detail=detail,
        now_iso=now_iso,
    )
