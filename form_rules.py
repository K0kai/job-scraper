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
    {"key": "city", "aliases": "city,cidade,cidade atual,current city,localidade", "mode": "text", "value_from": "candidate_city", "value": "", "sort_order": 50},
    {"key": "street_address", "aliases": "street address,address line,endereço,endereco,rua,logradouro,street name,nome da rua,address 1,address line 1", "mode": "text", "value_from": "candidate_street", "value": "", "sort_order": 50},
    {"key": "postal_code", "aliases": "zip,postal code,cep,codigo postal,código postal,postcode,zip code,código cep", "mode": "text", "value_from": "candidate_postal_code", "value": "", "sort_order": 51},
    {"key": "state", "aliases": "state,estado,province,provincia,província,uf,region,estado/provincia", "mode": "text", "value_from": "candidate_state", "value": "", "sort_order": 52},
    {"key": "country", "aliases": "country of origin,país de origem,pais de origem,country,país,pais,nationality country,select a country", "mode": "select", "value_from": "candidate_country", "value": "", "sort_order": 53},
    {"key": "current_company", "aliases": "current company,empresa atual,current employer,most recent employer,employer name,nome da empresa,company you work,where do you currently work,currently work", "mode": "text", "value_from": "candidate_current_company", "value": "", "sort_order": 54},
    {"key": "cpf", "aliases": "cpf,cadastro de pessoa física,documento,tax id,tax identification,id number,cpf number,número do documento", "mode": "text", "value_from": "candidate_cpf", "value": "", "sort_order": 56},
    {"key": "salary", "aliases": "salary,compensation,expected salary,salary expectation,faixa salarial,pretensão,pretensão salarial,remuneração,pay,salario,expectativa salarial", "mode": "salary", "value_from": "", "value": "", "sort_order": 60},
    {"key": "contract_type", "aliases": "contract type,type of contract,employment type,tipo de contratacao,contratacao,modelo de contratacao,regime", "mode": "select", "value_from": "candidate_contract_type", "value": "", "sort_order": 65},
    {"key": "cover_letter", "aliases": "cover letter,carta,carta de apresentação,message,mensagem,additional information,comments", "mode": "cover_letter", "value_from": "", "value": "", "sort_order": 70},
    {"key": "resume_file", "aliases": "resume,cv,curriculum,currículo,upload resume,anexar currículo,attach resume", "mode": "file", "value_from": "", "value": "", "sort_order": 80},
]


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", folded.casefold()).strip()


_FAKE_ADDRESS_RE = re.compile(
    r"\b(exemplo|example|sample|dummy|placeholder|lorem|teste|fake|"
    r"123\s*main|main\s*street|rua\s*exemplo|street\s*example|"
    r"endere[cç]o\s*exemplo|your\s*address\s*here)\b",
    re.I,
)


def looks_like_fake_address(text: str) -> bool:
    """Detecta endereço/CEP inventado ou placeholder — não deve ir para o formulário."""
    return bool(_FAKE_ADDRESS_RE.search(text or ""))


_EMPLOYER_SPLIT_RE = re.compile(r"\s+[—–]\s+")
_BR_HINT_RE = re.compile(
    r"\bbrazil\b|\bbrasil\b|\bbelo horizonte\b|\bsao paulo\b|\bsão paulo\b|"
    r"\brio de janeiro\b|\bcuritiba\b|\bbrasilia\b|\brasília\b|\brecife\b|\bporto alegre\b",
    re.I,
)


def extract_current_employer(resume_json: str | dict | None) -> str:
    """Primeiro empregador do currículo (objeto, string normalizada ou chave dedicada)."""
    data: dict | None = None
    if isinstance(resume_json, dict):
        data = resume_json
    elif isinstance(resume_json, str) and resume_json.strip():
        try:
            parsed = json.loads(resume_json)
            if isinstance(parsed, dict):
                data = parsed
        except (json.JSONDecodeError, ValueError, TypeError):
            data = None
    if not data:
        return ""
    dedicated = str(
        data.get("current_employer")
        or data.get("current_company")
        or data.get("employer")
        or ""
    ).strip()
    if dedicated:
        return dedicated
    experience = data.get("experience")
    if not isinstance(experience, list) or not experience:
        return ""
    first = experience[0]
    if isinstance(first, dict):
        return str(first.get("employer") or "").strip()
    text = str(first or "").strip()
    if not text:
        return ""
    parts = _EMPLOYER_SPLIT_RE.split(text, maxsplit=2)
    if len(parts) >= 2:
        employer = parts[1].split(":", 1)[0].strip()
        return employer
    return ""


def infer_candidate_country(
    cfg: dict[str, str] | None = None,
    resume_json: str | dict | None = None,
) -> str:
    """País do candidato: setting do painel, senão heurística BR a partir de cidade/CV."""
    cfg = cfg or {}
    explicit = str(cfg.get("candidate_country") or "").strip()
    if explicit:
        return explicit
    hay_bits = [
        str(cfg.get("candidate_city") or ""),
    ]
    data: dict | None = None
    if isinstance(resume_json, dict):
        data = resume_json
    elif isinstance(resume_json, str) and resume_json.strip():
        try:
            parsed = json.loads(resume_json)
            if isinstance(parsed, dict):
                data = parsed
        except (json.JSONDecodeError, ValueError, TypeError):
            data = None
    if data:
        hay_bits.append(str(data.get("location_notes") or ""))
        hay_bits.append(str(data.get("work_authorization_notes") or ""))
    hay = " ".join(hay_bits)
    if _BR_HINT_RE.search(hay):
        return "Brazil"
    return ""


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


def resolve_rule_value(
    rule: sqlite3.Row | dict,
    cfg: dict[str, str],
    *,
    cover_letter: str = "",
    resume_path: str = "",
    field_hint: str = "",
    job_text: str = "",
    resume_json: str | dict | None = None,
) -> str | None:
    """Valor a aplicar. `field_hint` = label+placeholder+name do campo na página;
    `job_text` = texto da vaga/página. O modo `salary` usa `field_hint` para a
    moeda e `job_text` para detectar CLT×PJ (detecção vence a preferência do painel)."""
    mode = str(rule["mode"])
    if mode == "skip":
        return None
    if mode == "cover_letter":
        return cover_letter
    if mode == "file":
        return resume_path
    if mode == "salary":
        from salary_policy import propose_salary

        prop = propose_salary(cfg, field_hint=field_hint, job_text=job_text)
        value = str(prop.get("formatted") or "")
        if not value:
            value = str(rule["value"] or "")
        return value
    value_from = str(rule["value_from"] or "").strip()
    key = str(rule.get("key") or "").casefold()
    if value_from:
        got = (cfg.get(value_from, "") or "").strip()
        if got:
            return got
    if key == "current_company":
        employer = extract_current_employer(resume_json)
        if employer:
            return employer
        return str(rule["value"] or "")
    if key == "country":
        country = infer_candidate_country(cfg, resume_json)
        if country:
            return country
        # Sem evidência: vazio (IA/humano perguntam) — não inventar Brazil.
        return ""
    if key == "state":
        return (cfg.get("candidate_state") or "").strip() or str(rule["value"] or "")
    if value_from:
        return str(rule["value"] or "")
    return str(rule["value"] or "")


# ---------------------------------------------------------------------------
# Expectativa salarial — BRL/USD nativos; outras moedas via FX (API + cache)
# ---------------------------------------------------------------------------

#: ordem importa: explicito no hint > preferência do painel > padrão BRL.
#: BRL primeiro — "R$ 0.000,00" tem '$', mas o R antes decide.
_USD_HINT_RE = re.compile(r"\busd\b|u\.s\.d|\bdollar|\bd[eé]llar|us\$|us-?d[oó]lar|(?<!R)\$", re.I)
_BRL_HINT_RE = re.compile(r"\bbrl\b|\br\$\b|reais|\breal\b|brazilian\s+real", re.I)
_OTHER_CURRENCY_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("COP", re.compile(r"\bcop\b|colombian\s+peso|pesos?\s+colomb", re.I)),
    ("EUR", re.compile(r"\beur\b|\beuro\b|€", re.I)),
    ("GBP", re.compile(r"\bgbp\b|\bpound\b|£|british\s+pound", re.I)),
    ("MXN", re.compile(r"\bmxn\b|mexican\s+peso|pesos?\s+mexican|pesos?\s+mx", re.I)),
    ("ARS", re.compile(r"\bars\b|argentine\s+peso|pesos?\s+argentin", re.I)),
    ("CLP", re.compile(r"\bclp\b|chilean\s+peso|pesos?\s+chilen", re.I)),
    ("PEN", re.compile(r"\bpen\b|\bsol(?:es)?\b|peruvian", re.I)),
    ("CAD", re.compile(r"\bcad\b|canadian\s+dollar", re.I)),
    ("AUD", re.compile(r"\baud\b|australian\s+dollar", re.I)),
)


def detect_salary_currency(*hints: str, preferred: str = "") -> str:
    """Código ISO da moeda a partir do texto do campo/vaga.

    'R$' → BRL; 'pesos colombianos' → COP; '$'/'USD' → USD. Sem evidência,
    respeita a preferência do painel; senão BRL.
    """
    hay = " ".join(h for h in hints if h)
    if _BRL_HINT_RE.search(hay):
        return "BRL"
    for code, pattern in _OTHER_CURRENCY_HINTS:
        if pattern.search(hay):
            return code
    if _USD_HINT_RE.search(hay):
        return "USD"
    pref = (preferred or "").strip().casefold()
    pref_map = {
        "usd": "USD", "dollar": "USD", "dólar": "USD", "dolares": "USD", "dinheiro": "USD",
        "brl": "BRL", "real": "BRL", "reais": "BRL",
        "cop": "COP", "eur": "EUR", "euro": "EUR", "gbp": "GBP", "mxn": "MXN",
        "ars": "ARS", "clp": "CLP", "pen": "PEN", "cad": "CAD", "aud": "AUD",
    }
    if pref in pref_map:
        return pref_map[pref]
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


#: sinais de regime de contratação na vaga/página — detecção vence a preferência.
_PJ_SIGNALS = (
    r"\bpj\b", r"pessoa jur[ií]dica", r"contractor", r"contract(or|ing)\b",
    r"self[- ]?employ", r"independent contractor", r"\bb2b\b", r"1099",
    r"freelanc", r"prestador de servi", r"aut[oô]nomo", r"como pessoa jur",
)
_CLT_SIGNALS = (
    r"\bclt\b", r"celetista", r"carteira assinada", r"\bemployee\b", r"empregad",
    r"efetivo", r"contrata[cç][aã]o\s+(clt|efetiv|com carteira)", r"\bw-?2\b",
    r"full[- ]time employee", r"regime clt", r"clt\b",
)


def detect_contract_regime(*texts: str) -> str | None:
    """'PJ' | 'CLT' | None a partir do texto da vaga/página (majoria de sinais).

    None = indefinido — quem decide é a preferência do painel. Empate = None.
    """
    hay = " ".join(t for t in texts if t).casefold()
    if not hay.strip():
        return None
    pj = sum(1 for pat in _PJ_SIGNALS if re.search(pat, hay))
    clt = sum(1 for pat in _CLT_SIGNALS if re.search(pat, hay))
    if pj > clt:
        return "PJ"
    if clt > pj:
        return "CLT"
    return None


def job_context_text(job: dict | None, page_text: str = "") -> str:
    """Título+companhia+descrição da vaga (+texto da página) p/ detecção de regime."""
    job = job or {}
    parts = [
        str(job.get("title") or ""),
        str(job.get("location") or ""),
        str(job.get("description") or "")[:4000],
        page_text[:2000],
    ]
    return " ".join(p for p in parts if p)


def format_money(amount: float, currency: str) -> str:
    """Formata valor arredondado com símbolo/código da moeda."""
    code = (currency or "USD").upper()
    n = max(0, int(round(amount)))
    if code == "BRL":
        return f"R$ {n:,}".replace(",", ".")
    if code == "USD":
        return f"$ {n:,}"
    if code == "EUR":
        return f"€ {n:,}".replace(",", " ")
    if code == "GBP":
        return f"£ {n:,}"
    if code == "COP":
        return f"COP {n:,}".replace(",", ".")
    return f"{code} {n:,}"


def salary_for(cfg: dict[str, str], currency: str, *, regime: str | None = None) -> str:
    """Pretensão do painel na moeda pedida.

    BRL/USD: campos nativos (BRL aplica multiplicador PJ). Outras moedas: FX
    automático (USD do painel → alvo; se só BRL, BRL → alvo via API).
    """
    code = (currency or "BRL").upper().strip() or "BRL"
    if code == "USD":
        raw = (cfg.get("salary_expectation_usd") or "").strip()
        return format_usd(raw) if raw else ""
    if code == "BRL":
        raw = (cfg.get("salary_expectation_brl") or "").strip()
        if not raw:
            return ""
        base = _parse_amount(raw)
        if base is None or base <= 0:
            return format_brl(raw)
        contractor = (regime == "PJ") if regime else _prefers_contractor(cfg)
        if contractor:
            base = base * _pj_multiplier(cfg)
        return format_brl(str(base))

    from fx_rates import convert_amount

    usd_raw = (cfg.get("salary_expectation_usd") or "").strip()
    brl_raw = (cfg.get("salary_expectation_brl") or "").strip()
    amount = None
    src = "USD"
    if usd_raw:
        amount = _parse_amount(usd_raw)
        src = "USD"
    elif brl_raw:
        amount = _parse_amount(brl_raw)
        src = "BRL"
        if amount and amount > 0:
            contractor = (regime == "PJ") if regime else _prefers_contractor(cfg)
            if contractor:
                amount = amount * _pj_multiplier(cfg)
    if amount is None or amount <= 0:
        return ""
    converted = convert_amount(amount, src, code, cfg=cfg)
    if converted is None or converted <= 0:
        return ""
    return format_money(converted, code)


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
