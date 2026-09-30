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
    {"key": "cpf", "aliases": "cpf,cadastro de pessoa física,documento,tax id,tax identification,id number,cpf number,número do documento", "mode": "text", "value_from": "candidate_cpf", "value": "", "sort_order": 55},
    {"key": "salary", "aliases": "salary,compensation,expected salary,salary expectation,faixa salarial,pretensão,pretensão salarial,remuneração,pay,salario,expectativa salarial", "mode": "salary", "value_from": "", "value": "", "sort_order": 60},
    {"key": "contract_type", "aliases": "contract type,type of contract,employment type,tipo de contratacao,contratacao,modelo de contratacao,regime", "mode": "select", "value_from": "candidate_contract_type", "value": "", "sort_order": 65},
    {"key": "cover_letter", "aliases": "cover letter,carta,carta de apresentação,message,mensagem,additional information,comments", "mode": "cover_letter", "value_from": "", "value": "", "sort_order": 70},
    {"key": "resume_file", "aliases": "resume,cv,curriculum,currículo,upload resume,anexar currículo,attach resume", "mode": "file", "value_from": "", "value": "", "sort_order": 80},
]


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", folded.casefold()).strip()


def ensure_default_rules(db: sqlite3.Connection) -> None:
    """Insere regras faltantes; migra a regra `salary` legada para modo `salary`.

    O modo `salary` escolhe BRL×USD pela moeda inferida do campo/vaga; no select
    nativo ele preserva o `value` legado como preferência.
    """
    existing = {str(r["key"]).casefold() for r in db.execute("SELECT key FROM form_field_rules")}
    for rule in DEFAULT_RULES:
        if rule["key"].casefold() in existing:
            continue
        db.execute(
            """INSERT INTO form_field_rules(key,aliases,mode,value_from,value,sort_order)
               VALUES(?,?,?,?,?,?)""",
            (rule["key"], rule["aliases"], rule["mode"], rule["value_from"], rule["value"], rule["sort_order"]),
        )
    db.execute(
        "UPDATE form_field_rules SET mode='salary' WHERE key='salary' AND mode='select'"
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
        if mode not in {"text", "select", "salary", "file", "cover_letter", "skip"}:
            mode = "text"
        value = form.get(f"rule_value_{idx}") or ""
        value_from = (form.get(f"rule_value_from_{idx}") or "").strip()
        db.execute(
            """INSERT INTO form_field_rules(key,aliases,mode,value_from,value,sort_order)
               VALUES(?,?,?,?,?,?)""",
            (rule_key, aliases, mode, value_from, value, order),
        )
        order += 10


def resolve_rule_value(rule: sqlite3.Row | dict, cfg: dict[str, str], *, cover_letter: str = "", resume_path: str = "", field_hint: str = "") -> str | None:
    """Valor a aplicar. `field_hint` = label+placeholder+name do campo na página
    (usado pelo modo `salary` para inferir a moeda esperada)."""
    mode = str(rule["mode"])
    if mode == "skip":
        return None
    if mode == "cover_letter":
        return cover_letter
    if mode == "file":
        return resume_path
    if mode == "salary":
        currency = detect_salary_currency(field_hint, preferred=cfg.get("salary_currency_preference", ""))
        value = salary_for(cfg, currency)
        if not value and currency == "USD":  # so temos BRL configurado
            value = salary_for(cfg, "BRL")
        return value or str(rule["value"] or "")
    value_from = str(rule["value_from"] or "").strip()
    if value_from:
        return cfg.get(value_from, "") or str(rule["value"] or "")
    return str(rule["value"] or "")


# ---------------------------------------------------------------------------
# Expectativa salarial — BRL × USD escolhidos pela moeda inferida do campo/vaga
# ---------------------------------------------------------------------------

#: ordem importa: explicito no hint > preferência do painel > padrão BRL.
#: BRL primeiro — "R$ 0.000,00" tem '$', mas o R antes decide.
_USD_HINT_RE = re.compile(r"\busd\b|u\.s\.d|\bdollar|\bd[eé]llar|us\$|us-?d[oó]lar|(?<!R)\$", re.I)
_BRL_HINT_RE = re.compile(r"\bbrl\b|\br\$\b|reais|\breal\b|brazilian\s+real", re.I)


def detect_salary_currency(*hints: str, preferred: str = "") -> str:
    """'USD' | 'BRL' a partir do texto do campo/vaga (label, placeholder, name).

    Placeholder 'R$ 0.000,00' → BRL; '$'/'USD'/'annual' → USD. Sem evidência,
    respeita a preferência do painel; senão BRL (candidatura majoritariamente BR).
    """
    hay = " ".join(h for h in hints if h)
    if _BRL_HINT_RE.search(hay):
        return "BRL"
    if _USD_HINT_RE.search(hay):
        return "USD"
    pref = (preferred or "").strip().casefold()
    if pref in {"usd", "dollar", "dólar", "dolares", "dinheiro"}:
        return "USD"
    if pref in {"brl", "real", "reais"}:
        return "BRL"
    return "BRL"


def _parse_amount(value: str) -> float | None:
    """'6000' | '6.000' | 'R$ 6.000,50' | '80,000' | '80000.00' -> número.

    Decide o separador decimal pelo ÚLTIMO separador e usa o tamanho do sufixo
    para distinguir milhar de decimal ('6.000' = milhar, '6000.50' = decimal).
    """
    cleaned = re.sub(r"[^\d.,]", "", value or "")
    if not cleaned:
        return None
    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")
    if last_comma > last_dot:
        head = cleaned[:last_comma].replace(".", "")
        tail = cleaned[last_comma + 1:]
    elif last_dot > last_comma:
        head = cleaned[:last_dot].replace(",", "")
        tail = cleaned[last_dot + 1:]
    else:
        head, tail = cleaned.replace(",", "").replace(".", ""), ""
    if len(tail) == 3 and tail.isdigit() and head:  # '80,000' / '6.000' = milhar
        head, tail = head + tail, ""
    try:
        return float(f"{head}.{tail}") if tail else float(head)
    except ValueError:
        return None


def format_brl(value: str) -> str:
    """'6000' / '6000.50' → 'R$ 6.000' (centavos arredondados)."""
    number = _parse_amount(value)
    if number is None or number <= 0:
        return (value or "").strip()
    return f"R$ {round(number):,}".replace(",", ".")


def format_usd(value: str) -> str:
    """'80000' / 'US$ 80,000' → '$ 80,000' (sem centavos quando inteiro)."""
    number = _parse_amount(value)
    if number is None or number <= 0:
        return (value or "").strip()
    if number == int(number):
        return f"$ {int(number):,}"
    return f"$ {number:,.2f}"


def _pj_multiplier(cfg: dict[str, str]) -> float:
    """Fator PJ (aceita decimal e vírgula); ausente/inválido → 1.0 (sem ajuste)."""
    raw = (cfg.get("salary_pj_multiplier") or "").strip().replace(",", ".")
    try:
        factor = float(raw)
    except ValueError:
        return 1.0
    return factor if factor > 0 else 1.0


def _prefers_contractor(cfg: dict[str, str]) -> bool:
    pref = (cfg.get("candidate_contract_type") or "").strip().casefold()
    return pref in {"contractor", "pj", "autonomo", "autônomo", "prestador", "freelance"}


def salary_for(cfg: dict[str, str], currency: str) -> str:
    """Pretensão do painel na moeda pedida. BRL = base CLT; se a preferência de
    contratação for PJ, aplica o multiplicador (SÓ faz sentido em reais/Brasil).
    USD ignora o multiplicador. Vazio = não configurado."""
    if currency == "USD":
        raw = (cfg.get("salary_expectation_usd") or "").strip()
        return format_usd(raw) if raw else ""
    raw = (cfg.get("salary_expectation_brl") or "").strip()
    if not raw:
        return ""
    base = _parse_amount(raw)
    if base is None or base <= 0:
        return format_brl(raw)
    if _prefers_contractor(cfg):
        base = base * _pj_multiplier(cfg)
    return format_brl(str(base))


def format_cpf(value: str) -> str:
    """Somente dígitos, ou '000.000.000-00' quando o campo pede máscara.

    Retorna (sem máscara) se não houver 11 dígitos — melhor deixar o humano ver.
    """
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) != 11:
        return digits or (value or "").strip()
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def prepare_text_value(rule: sqlite3.Row | dict, value: str, field_hint: str = "") -> str:
    """Ajusta o valor resolvido ao formato que o campo espera (máscaras etc.)."""
    text = str(value or "")
    if str(rule.get("key") or "").casefold() == "cpf" and re.sub(r"\D", "", text):
        # placeholder tipo '000.000.000-00' pede a versão mascarada
        masked_hint = bool(re.search(r"0{3}[.\s]?0{3}[.\s]?0{3}[-.\s]?0{2}", field_hint or ""))
        digits = re.sub(r"\D+", "", text)
        return format_cpf(digits) if masked_hint and len(digits) == 11 else digits
    return text


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
