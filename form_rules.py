# Regras configuráveis para preenchimento de formulários de candidatura.
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from typing import Any


DEFAULT_RULES: list[dict[str, Any]] = [
    {"key": "full_name", "aliases": "name,full_name,fullname,nome completo,your name,candidate name", "mode": "text", "value_from": "candidate_name", "value": "", "sort_order": 10},
    {"key": "email", "aliases": "email,e-mail,mail,correo", "mode": "text", "value_from": "candidate_email", "value": "", "sort_order": 20},
    {"key": "phone", "aliases": "phone,telephone,tel,telefone,celular,mobile,whatsapp", "mode": "text", "value_from": "candidate_phone", "value": "", "sort_order": 30},
    {"key": "linkedin", "aliases": "linkedin,linkedin url,profile url", "mode": "text", "value_from": "candidate_linkedin", "value": "", "sort_order": 40},
    {"key": "city", "aliases": "city,cidade,location,localidade,endereço,address", "mode": "text", "value_from": "candidate_city", "value": "", "sort_order": 50},
    {"key": "salary", "aliases": "salary,compensation,expected salary,faixa salarial,pretensão,remuneração,pay", "mode": "select", "value_from": "", "value": "", "sort_order": 60},
    {"key": "cover_letter", "aliases": "cover letter,carta,carta de apresentação,message,mensagem,additional information,comments", "mode": "cover_letter", "value_from": "", "value": "", "sort_order": 70},
    {"key": "resume_file", "aliases": "resume,cv,curriculum,currículo,upload resume,anexar currículo,attach resume", "mode": "file", "value_from": "", "value": "", "sort_order": 80},
]


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", folded.casefold()).strip()


def ensure_default_rules(db: sqlite3.Connection) -> None:
    count = db.execute("SELECT COUNT(*) AS n FROM form_field_rules").fetchone()["n"]
    if count:
        return
    for rule in DEFAULT_RULES:
        db.execute(
            """INSERT INTO form_field_rules(key,aliases,mode,value_from,value,sort_order)
               VALUES(?,?,?,?,?,?)""",
            (rule["key"], rule["aliases"], rule["mode"], rule["value_from"], rule["value"], rule["sort_order"]),
        )


def list_rules(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(db.execute("SELECT * FROM form_field_rules ORDER BY sort_order, id"))


def save_rules_from_form(db: sqlite3.Connection, form: dict[str, str]) -> None:
    """Espera campos rule_key_N, rule_aliases_N, rule_mode_N, rule_value_N, rule_value_from_N."""
    ids: set[int] = set()
    for key in form:
        match = re.fullmatch(r"rule_key_(\d+)", key)
        if match:
            ids.add(int(match.group(1)))
    db.execute("DELETE FROM form_field_rules")
    order = 10
    for idx in sorted(ids):
        rule_key = (form.get(f"rule_key_{idx}") or "").strip()
        if not rule_key:
            continue
        aliases = (form.get(f"rule_aliases_{idx}") or "").strip()
        mode = (form.get(f"rule_mode_{idx}") or "text").strip().casefold()
        if mode not in {"text", "select", "file", "cover_letter", "skip"}:
            mode = "text"
        value = form.get(f"rule_value_{idx}") or ""
        value_from = (form.get(f"rule_value_from_{idx}") or "").strip()
        db.execute(
            """INSERT INTO form_field_rules(key,aliases,mode,value_from,value,sort_order)
               VALUES(?,?,?,?,?,?)""",
            (rule_key, aliases, mode, value_from, value, order),
        )
        order += 10


def resolve_rule_value(rule: sqlite3.Row | dict, cfg: dict[str, str], *, cover_letter: str = "", resume_path: str = "") -> str | None:
    mode = str(rule["mode"])
    if mode == "skip":
        return None
    if mode == "cover_letter":
        return cover_letter
    if mode == "file":
        return resume_path
    value_from = str(rule["value_from"] or "").strip()
    if value_from:
        return cfg.get(value_from, "") or str(rule["value"] or "")
    return str(rule["value"] or "")


def find_rule_for_label(label: str, rules: list[sqlite3.Row] | list[dict]) -> dict | sqlite3.Row | None:
    hay = normalize(label)
    if not hay:
        return None
    best = None
    best_len = 0
    for rule in rules:
        for alias in str(rule["aliases"] or "").split(","):
            alias_n = normalize(alias)
            if not alias_n:
                continue
            if alias_n in hay or hay in alias_n:
                if len(alias_n) > best_len:
                    best = rule
                    best_len = len(alias_n)
    return best


def pick_select_option(options: list[str], preferred: str) -> str | None:
    pref = normalize(preferred)
    if not pref or not options:
        return None
    exact = [opt for opt in options if normalize(opt) == pref]
    if exact:
        return exact[0]
    contains = [opt for opt in options if pref in normalize(opt) or normalize(opt) in pref]
    if contains:
        return contains[0]
    pref_tokens = set(pref.split())
    scored: list[tuple[int, str]] = []
    for opt in options:
        tokens = set(normalize(opt).split())
        scored.append((len(pref_tokens & tokens), opt))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    return None


def rules_as_json(rules: list[sqlite3.Row]) -> str:
    payload = [
        {
            "id": row["id"],
            "key": row["key"],
            "aliases": row["aliases"],
            "mode": row["mode"],
            "value_from": row["value_from"],
            "value": row["value"],
            "sort_order": row["sort_order"],
        }
        for row in rules
    ]
    return json.dumps(payload, ensure_ascii=False)
