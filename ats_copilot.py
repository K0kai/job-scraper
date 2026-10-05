"""Copiloto de IA com override completo do fluxo quando o robo trava.

Quando qualquer handler ATS ou o Easy Apply do LinkedIn se ve num estado que
nao sabe tratar (campo obrigatorio sem regra, wizard travado, site sem driver),
este modulo assume o comando: a IA recebe TODAS as informacoes do candidato
(perfil + dossier do curriculo analisado), da vaga e da pagina ao vivo (campos,
botoes, texto, indices de interacao) e devolve UM comando por turno - mover
mouse, clicar, digitar, selecionar, marcar, abrir/fechar aba, rodar JS.

Override total: a partir do momento em que e chamado, quem decide os proximos
passos e a IA. Se ela declarar que destravou (`done`), devolvemos o fluxo ao
roteador (que entao aplica a politica normal de submit — no Easy Apply o
copiloto NUNCA clica envio final). Em site **sem driver ATS**,
``allow_submit=True`` e o copiloto deve concluir Next…Submit sozinho.
Se ela desistir (`abort`), ou nao houver IA, ou esgotar os turnos sem progresso,
FECHAMOS a pagina e marcamos a vaga para nota AMARELA.

As partes puras (montar prompt, extrair a acao JSON, escolher o alvo de clique)
sao separadas do executor para testarmos sem navegador.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time

from ats_kernel import MARK_FIELD_ATTR, collect_fields, field_selector, safe_fill, safe_select
from browser_engine import click_scanned_button, scan_buttons
from resume_pipeline import AiUnavailableError

LOG = logging.getLogger("job-scraper")

#: prefixo da nota que o painel pinta em AMARELO ("nao foi possivel automatizar")
COPILOT_FAIL_PREFIX = "COPILOT_UNAUTOMATED:"

#: estado do copiloto -> roteador/chamador decide o que fazer
SOLVED = "solved"      # IA destravou o fluxo; pode retomar o preenchimento/envio
ABORTED = "aborted"    # IA desistiu (ou sem IA/esgotou turnos): fechar + nota amarela
UNAVAILABLE = "unavailable"  # IA nao configurada: segue o comportamento antigo (humano)

MAX_TURNS = 10  # free tier: folga p/ wizards longos; estagnação / handoff cortam desperdício
_TURN_PAUSE = (0.35, 0.9)
#: teto de espera acumulada em retries de rate-limit (429/quota) no mesmo takeover
AI_RATE_LIMIT_BUDGET_SECONDS = 600
#: turnos seguidos sem progresso → abort (em vez de gastar o teto inteiro)
STAGNATION_LIMIT = 4
#: ações por turno (1 chamada de IA → N passos)
MAX_STEPS_PER_TURN = 5

#: nunca clicados pelo copiloto - envio de candidatura fica com a politica do handler
_SUBMIT_BLOCK_RE = re.compile(
    r"submit|apply now|finalizar|enviar candidatura|complete application|send application",
    re.I,
)
#: avanço seguro (Next/Continue/Review) — bot retoma depois do handoff
_ADVANCE_RE = re.compile(
    r"\b(next|continue|próximo|proximo|continuar|avançar|avancar|seguinte|review|revisar|rever)\b",
    re.I,
)
#: envio final (só quando allow_submit — site sem driver)
_SUBMIT_CLICK_RE = re.compile(
    r"submit(\s+application)?|send\s+application|complete\s+application|"
    r"enviar(\s+a)?\s+candidatura|finalizar(\s+a)?\s+candidatura|"
    r"^apply$|candidatar|enviar$",
    re.I,
)
_PROGRESS_HINT_RE = re.compile(
    r"-> (typed|selected|clicked|toggled|ran js|uploaded)|ask -> answered",
    re.I,
)

#: estado extra: candidatura enviada pelo copiloto (site sem handler)
SUBMITTED = "submitted"

# vocabulario de comandos oferecido a IA (tudo que o bot sabe executar)
ACTION_VOCAB = """
You are taking FULL control of a browser automation that got STUCK on a job
application page. Decide the next step(s). Reply with ONLY a JSON object:

  {"action": "<name>", "args": { ... }, "reason": "<short why>"}

Or a short batch (preferred when several fills/clicks are obvious):

  {"action":"batch","args":{"steps":[{"action":"...","args":{...}}, ...]}, "reason":"..."}

Max 5 steps per batch. Available actions (args in parentheses):
  click    (field=<idx> | button=<idx> | selector="<css>" | x=<int> y=<int>)
  type     (field=<idx> | selector="<css>", text="<string>")
  select   (field=<idx> | selector="<css>", value="<option text>")   # native <select>
  check    (field=<idx> | selector="<css>", checked=<true|false>)    # radio/checkbox
  upload   (field=<idx> | selector="<css>")                          # attach RESUME FILE (panel PDF)
  mouse    (x=<int>, y=<int>)                                        # hover to reveal UI
  press    (key="<Enter|Tab|Escape|...>")
  scroll   (y=<int>)                                                 # pixels, +down -up
  wait     (seconds=<number>)                                        # let a SPA re-render
  open     (url="<https://...>")                                     # open a new tab
  close    ()                                                        # close the active tab
  js       (code="<inline JS, no return stmt, use `document`>")
  done     ()                                                        # unblocked; hand back to caller
  ask      (question="<ONE concrete question naming the exact field label>")
  abort    (reason="<why it cannot be automated>")                   # give up -> yellow note

Rules:
- HARD RULE — NEVER INVENT DATA. If a value is not explicitly present in
  PANEL PROFILE, CANDIDATE FACTS, or RESUME EXCERPT, you MUST emit `ask` as a
  STANDALONE action (never bury `ask` inside a batch). This applies to ANY
  field the site asks for: address, CEP/zip, phone, city, state, country,
  salary nuance, dates, IDs, skill ratings you cannot evidence, open questions,
  and every other blank. Guessing, placeholders ("Rua Exemplo", "123 Main St",
  fake CEP, "N/A", "TBD"), or copying the job's company/location is FORBIDDEN.
  When unsure even a little → `ask`. Do not probe with wait/scroll/mouse first.
- HARD RULE — `ask.question` MUST be ONE specific question a human can answer
  in a single reply. ALWAYS quote the exact on-page field label (from FIELDS),
  e.g. `O formulário pede «Em uma escala de 1-10, avalie Python». Qual nota
  (1–10) devo marcar?` or `Qual CEP preencher em «Postal code»?`.
  FORBIDDEN vague asks: "required fields need confirmation", "subjective
  ratings", "custom form needs input", "preciso de um dado", or any summary
  that does not name the field. Ask ONE field at a time; never bundle.
- "Add skills" / "Search and add skills" / typeahead skill pickers: these are
  NOT 1–10 rating scales. Type skill names from RESUME EXCERPT / FACTS (one
  at a time, Enter between chips if needed). Do NOT `ask` "qual valor
  preencher" for addSkills widgets. Only `ask` if the page requires a skill
  you cannot evidence at all — then list 3–5 concrete skills to choose from.
- Be frugal: prefer ONE batch that fills/clicks only what you already know, then
  click Next/Continue/Review (never Submit) OR emit `done` so the bot resumes.
  If you cannot unblock after asking, `abort` quickly (do not burn turns guessing).
- After filling required fields, prefer clicking Next/Continue/Review yourself
  in the same batch, or `done` — do not keep typing the same fields.
- NEVER emit a click whose target text matches submit/apply/finalizar/enviar. The
  caller handles the real submission; you only unblock the flow.
- Prefer `field`/`button` indices from the page snapshot; they are stable handles.
- Resume/CV upload: if RESUME FILE path is listed below, use `upload` on the file
  input. NEVER ask the human for a resume/CV — it is already on disk from the panel.
- Fill what you can from CANDIDATE FACTS + PANEL PROFILE + RESUME EXCERPT. Prefer
  PANEL PROFILE for city/country/state/street/postal/salary/contact; prefer RESUME
  EXCERPT for education / location_notes / work_authorization / current employer.
  Country / State / Province / País / Estado / UF: use PANEL PROFILE
  `country`/`state` (or an explicit line in FACTS/EXCERPT). If those are missing,
  you MUST `ask` — do NOT invent Brazil, a Brazilian state, or the job-posting
  country. City alone is NOT enough to fill country/state unless the text
  explicitly names them.
  Street / address line / CEP / zip / postal code: use PANEL PROFILE `street` and
  `postal_code` only. NEVER type placeholders. If missing → `ask`.
  Skill self-ratings (scale 1–10): use `select` on the numeric option that matches
  the resume; be conservative; if unsure, `ask`.
  Current company / employer: use RESUME EXCERPT "Current employer" or PANEL
  PROFILE; do NOT use JOB.company (that is the hiring company).
  For salary fields: panel salary_brl/usd are MONTHLY mid-level targets.
  Convert FX and match the field period (year/month/hour).
  If the job is internship/estágio/trainee/junior/entry-level and the panel
  figure is unrealistic, LOWER it (including BELOW the panel floor) to a
  plausible stipend/junior band for that market — never invent luxury pay.
  For mid+ roles: stay above Brazil floor (converted) and a bit under local
  market (~10–20%) for offshore hires; prefer panel USD remote anchor when
  present. Do not put N/A if a base salary exists.
  If a required field is still missing, you MUST `ask` (human answers in the web
  panel; Chrome stays open). Only `abort` when the human cancelled/timed out, or
  for legal consent you must not sign. Never invent.
- For diversity/identity questions use only values you are explicitly given; if a
  legal consent box is the only blocker, use `abort` (we never sign terms for the
  person).
- When the flow looks unblocked (a Continue/Next is now clickable, required fields
  are filled), emit `done`.
""".strip()

_PANEL_PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("candidate_name", "name"),
    ("candidate_email", "email"),
    ("candidate_phone", "phone"),
    ("candidate_linkedin", "linkedin"),
    ("candidate_city", "city"),
    ("candidate_street", "street"),
    ("candidate_postal_code", "postal_code"),
    ("candidate_state", "state"),
    ("candidate_country", "country"),
    ("candidate_current_company", "current_company"),
    ("candidate_cpf", "cpf"),
    ("salary_expectation_brl", "salary_brl"),
    ("salary_expectation_usd", "salary_usd"),
    ("candidate_contract_type", "contract_type"),
)

_STREET_FIELD_RE = re.compile(
    r"street\s*address|address\s*line|endere[cç]o|logradouro|\brua\b|"
    r"street\s*name|nome\s*da\s*rua|address\s*1|line\s*1|\baddress\b(?!\s*(type|book))",
    re.I,
)
_POSTAL_FIELD_RE = re.compile(
    r"\bzip\b|postal\s*code|\bcep\b|post\s*code|postcode|"
    r"c[oó]digo\s*postal|codigo\s*postal",
    re.I,
)
_PHONE_FIELD_RE = re.compile(r"phone|telefone|celular|mobile|whatsapp|\btel\b", re.I)
_EMAIL_FIELD_RE = re.compile(r"e-?mail|correo", re.I)
_NAME_FIELD_RE = re.compile(
    r"full\s*name|nome\s*completo|your\s*name|candidate\s*name|"
    r"^(name|nome)$|first\s*name|last\s*name|sobrenome|primeiro\s*nome",
    re.I,
)
_CITY_FIELD_RE = re.compile(r"\bcity\b|\bcidade\b|localidade", re.I)
_STATE_FIELD_RE = re.compile(
    r"\bstate\b|\bestado\b|\bprovince\b|prov[ií]ncia|\buf\b|estado/provincia",
    re.I,
)
_COUNTRY_FIELD_RE = re.compile(r"\bcountry\b|\bpa[ií]s\b|nationality\s*country", re.I)
_CPF_FIELD_RE = re.compile(r"\bcpf\b|tax\s*id|documento|id\s*number", re.I)
_LINKEDIN_FIELD_RE = re.compile(r"linkedin|profile\s*url", re.I)
_COMPANY_FIELD_RE = re.compile(
    r"current\s*company|empresa\s*atual|current\s*employer|most\s*recent\s*employer",
    re.I,
)

# (profile_key, kind, matcher, ask_question)
_PANEL_FIELD_SPECS: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "candidate_street",
        "street",
        _STREET_FIELD_RE,
        "Qual é o seu endereço (rua e número)? (será salvo no perfil)",
    ),
    (
        "candidate_postal_code",
        "postal",
        _POSTAL_FIELD_RE,
        "Qual é o seu CEP / código postal? (será salvo no perfil)",
    ),
    (
        "candidate_phone",
        "phone",
        _PHONE_FIELD_RE,
        "Qual é o seu telefone (com DDD)? (será salvo no perfil)",
    ),
    (
        "candidate_email",
        "email",
        _EMAIL_FIELD_RE,
        "Qual é o seu e-mail? (será salvo no perfil)",
    ),
    (
        "candidate_name",
        "name",
        _NAME_FIELD_RE,
        "Qual é o seu nome completo? (será salvo no perfil)",
    ),
    (
        "candidate_city",
        "city",
        _CITY_FIELD_RE,
        "Qual é a sua cidade? (será salva no perfil)",
    ),
    (
        "candidate_state",
        "state",
        _STATE_FIELD_RE,
        "Qual é o seu estado / UF / província? (será salvo no perfil)",
    ),
    (
        "candidate_country",
        "country",
        _COUNTRY_FIELD_RE,
        "Qual é o seu país? (será salvo no perfil)",
    ),
    (
        "candidate_cpf",
        "cpf",
        _CPF_FIELD_RE,
        "Qual é o seu CPF / documento? (será salvo no perfil)",
    ),
    (
        "candidate_linkedin",
        "linkedin",
        _LINKEDIN_FIELD_RE,
        "Qual é a URL do seu LinkedIn? (será salva no perfil)",
    ),
    (
        "candidate_current_company",
        "current_company",
        _COMPANY_FIELD_RE,
        "Qual é a sua empresa atual / último empregador? (será salva no perfil)",
    ),
)


def _field_hay(field: dict | None = None, *, label: str = "") -> str:
    bits = [
        label,
        str((field or {}).get("label") or ""),
        str((field or {}).get("placeholder") or ""),
        str((field or {}).get("name") or ""),
        str((field or {}).get("id") or ""),
    ]
    return " ".join(bits)


def match_panel_field_spec(field: dict | None = None, *, label: str = "") -> tuple[str, str, str] | None:
    """Retorna (profile_key, kind, ask_question) se o campo casa com dado do painel."""
    hay = _field_hay(field, label=label)
    if not hay.strip():
        return None
    for profile_key, kind, matcher, question in _PANEL_FIELD_SPECS:
        if matcher.search(hay):
            return profile_key, kind, question
    return None


def field_address_kind(field: dict | None = None, *, label: str = "") -> str | None:
    """'street' | 'postal' | None a partir do label/placeholder/name do campo."""
    spec = match_panel_field_spec(field, label=label)
    if not spec:
        return None
    _key, kind, _q = spec
    return kind if kind in {"street", "postal"} else None


def address_ask_question(kind: str) -> str:
    for _key, k, _matcher, question in _PANEL_FIELD_SPECS:
        if k == kind:
            return question
    return "Preciso de um dado do seu perfil para preencher este campo. Pode informar?"


def list_missing_panel_asks(cfg: dict | None, fields: list[dict]) -> list[str]:
    """Perguntas obrigatórias: campos da página cujo valor no painel está vazio."""
    cfg = cfg or {}
    seen_keys: set[str] = set()
    questions: list[str] = []
    for field in fields or []:
        spec = match_panel_field_spec(field)
        if not spec:
            continue
        profile_key, _kind, base_q = spec
        if profile_key in seen_keys:
            continue
        if str(cfg.get(profile_key) or "").strip():
            continue
        seen_keys.add(profile_key)
        label = str(field.get("label") or "").strip() or str(field.get("placeholder") or "").strip()
        if label:
            questions.append(f"O formulário pede «{_clip(label, 120)}». {base_q}")
        else:
            questions.append(base_q)
    return questions


_VAGUE_ASK_RE = re.compile(
    r"required\s+fields?|human\s+confirmation|avoid\s+guessing|specific\s+input|"
    r"subjective\s+ratings?|custom\s+application\s+form|needs?\s+human|"
    r"dados?\s+(faltantes?|necess|pendente|adicionais)|"
    r"precisa(mos)?\s+de\s+(um\s+)?dado|informa[cç][oõ]es?\s+adicionais|"
    r"missing\s+data|confirmation\s+to\s+avoid|sem\s+chutar|"
    r"preencher\s+campos?\s+obrigat|campos?\s+obrigat[oó]rios?|"
    r"regra\s+estrita|confirma[cç][aã]o\s+humana|pr[eé]-?definido|"
    r"escala\s*/\s*profici[eê]ncia|profici[eê]ncia\s+exige|"
    r"valor\s+exato|n[aã]o\s+houver\s+valor|diga\s+o\s+r[oó]tulo\s+exato|"
    r"r[oó]tulo\s+exato\s+do\s+campo",
    re.I,
)


def is_vague_ask(question: str) -> bool:
    """True se a pergunta não aponta um campo concreto que o humano consiga responder."""
    q = (question or "").strip()
    if len(q) < 12:
        return True
    if _VAGUE_ASK_RE.search(q):
        return True
    # Typeahead "add skills" / rótulos técnicos — não é pergunta útil ao humano.
    if _ADD_SKILLS_WIDGET_RE.search(q):
        return True
    # Sem rótulo citado e sem "qual …" direcionado → provavelmente vago.
    has_quoted_label = bool(re.search(r"[«»\"“”'].{3,}", q))
    has_direct_qual = bool(
        re.search(
            r"\bqual\b|\bquais\b|\bwhat\b|\bwhich\b|\binforme\b|\bdigite\b|"
            r"\bpreencha\b|\bcep\b|\bendere[cç]o\b|\btelefone\b|\be-?mail\b|"
            r"\bnota\b|\bop[cç][aã]o\b",
            q,
            re.I,
        )
    )
    if has_quoted_label and has_direct_qual:
        return False
    if not has_quoted_label and (not has_direct_qual or len(q) > 100):
        return True
    return False


def _field_label(field: dict) -> str:
    return (
        str(field.get("label") or "").strip()
        or str(field.get("placeholder") or "").strip()
        or str(field.get("name") or "").strip()
        or str(field.get("id") or "").strip()
    )


def _ask_for_page_field(field: dict) -> str:
    """Pergunta concreta citando o rótulo (e opções, se houver)."""
    label = _field_label(field) or "campo sem rótulo"
    opts = [str(o).strip() for o in (field.get("options") or []) if str(o).strip()]
    tag = str(field.get("tag") or "")
    ftype = str(field.get("type") or "").casefold()
    if opts:
        sample = ", ".join(opts[:10])
        more = "…" if len(opts) > 10 else ""
        return (
            f"O formulário pede «{_clip(label, 160)}». "
            f"Qual opção/nota marcar? Opções: {sample}{more}"
        )
    if tag == "input" and ftype == "number":
        return (
            f"O formulário pede «{_clip(label, 160)}». "
            f"Qual número preencher? (geralmente 1–10)"
        )
    return (
        f"O formulário pede «{_clip(label, 160)}». "
        f"Qual valor devo preencher nesse campo?"
    )


_ADD_SKILLS_WIDGET_RE = re.compile(
    r"add[_\s-]*skills?|search\s+and\s+add|adicion(ar|e)\s+(skills?|habilidades?)|"
    r"type\s*to\s*add|selecione\s+habilidades|addskills",
    re.I,
)


def _looks_like_add_skills_widget(field: dict) -> bool:
    """Typeahead 'Search and add skills' — NÃO é escala 1–10."""
    label = _field_label(field)
    name = str(field.get("name") or "")
    fid = str(field.get("id") or "")
    hay = " ".join((label, name, fid))
    return bool(_ADD_SKILLS_WIDGET_RE.search(hay))


def _iter_skill_scale_fields(fields: list[dict]) -> list[dict]:
    """Campos de escala/proficiência 1–10 (select, combobox ou number)."""
    try:
        from ats_answers import label_looks_like_skill_rating, options_look_like_numeric_scale
    except Exception:
        label_looks_like_skill_rating = None  # type: ignore
        options_look_like_numeric_scale = None  # type: ignore

    out: list[dict] = []
    for field in fields or []:
        if _looks_like_add_skills_widget(field):
            continue
        label = _field_label(field)
        opts = [str(o) for o in (field.get("options") or []) if str(o).strip()]
        tag = str(field.get("tag") or "")
        ftype = str(field.get("type") or "").casefold()
        looks_scale = False
        if callable(label_looks_like_skill_rating) and label_looks_like_skill_rating(label):
            looks_scale = True
        elif callable(options_look_like_numeric_scale) and options_look_like_numeric_scale(opts):
            looks_scale = True
        else:
            low = label.casefold()
            # Exige sinal de ESCALA — "skill"/"habilidade" sozinho é typeahead, não 1–10.
            has_scale_cue = any(
                tok in low
                for tok in (
                    "escala",
                    "scale",
                    "1-10",
                    "1 – 10",
                    "1 a 10",
                    "profici",
                    "rate ",
                    "rate your",
                    "avalie",
                    "autoavalia",
                    "self-assess",
                    "self assess",
                    "nível",
                    "nivel",
                )
            )
            has_topic = any(
                tok in low
                for tok in (
                    "experi",
                    "skill",
                    "habilidade",
                    "conheciment",
                    "familiar",
                )
            )
            if has_scale_cue and (has_topic or "1-10" in low or "1 a 10" in low or "1 – 10" in low):
                looks_scale = True
            if tag in {"select", "combobox"} and opts:
                nums = []
                for o in opts:
                    m = re.match(r"^\s*(\d{1,2})\s*$", o)
                    if m:
                        nums.append(int(m.group(1)))
                if len(nums) >= 3 and min(nums) >= 1 and max(nums) <= 10:
                    # Só trata como escala se o rótulo sugere rating OU opções são 1..N puro.
                    if has_scale_cue or has_topic or len(nums) >= 5:
                        looks_scale = True
            if tag == "input" and ftype == "number" and looks_scale:
                pass
        if looks_scale:
            out.append(field)
    return out


def concrete_ask_from_page(cfg: dict | None, fields: list[dict]) -> str | None:
    """Monta UMA pergunta concreta a partir dos campos vazios da página."""
    missing = list_missing_panel_asks(cfg, fields)
    if missing:
        return missing[0]

    # Escalas / proficiência primeiro (é o que a IA mais vagueia).
    for field in _iter_skill_scale_fields(fields):
        return _ask_for_page_field(field)

    # Campos required sem regra de painel (ignora typeahead add-skills).
    for field in fields or []:
        if _looks_like_add_skills_widget(field):
            continue
        if not field.get("required"):
            continue
        if not _field_label(field):
            continue
        return _ask_for_page_field(field)

    # Qualquer select/combobox com opções (ainda sem valor óbvio no perfil).
    for field in fields or []:
        if _looks_like_add_skills_widget(field):
            continue
        opts = [str(o) for o in (field.get("options") or []) if str(o).strip()]
        if str(field.get("tag") or "") in {"select", "combobox"} and opts and _field_label(field):
            return _ask_for_page_field(field)

    # Último recurso: primeiro campo com rótulo legível (não widget de skills).
    for field in fields or []:
        if _looks_like_add_skills_widget(field):
            continue
        label = _field_label(field)
        if len(label) >= 3:
            return _ask_for_page_field(field)
    return None


def clarify_ask_question(question: str, fields: list[dict], cfg: dict | None = None) -> str:
    """Reescreve asks vagos da IA em uma pergunta com rótulo do campo."""
    q = (question or "").strip()
    if q and not is_vague_ask(q):
        return q
    concrete = concrete_ask_from_page(cfg, fields)
    if concrete:
        LOG.info("copiloto: ask vago reescrito → %s", concrete[:160])
        return concrete
    # Nunca peça o rótulo ao humano — liste o que a página mostra.
    labels = []
    for field in fields or []:
        lab = _field_label(field)
        if lab and lab not in labels:
            labels.append(lab)
        if len(labels) >= 6:
            break
    if labels:
        listed = "; ".join(f"«{_clip(lab, 80)}»" for lab in labels)
        return (
            "Não consegui identificar sozinho o campo faltante. "
            f"Qual destes preencher e com qual valor? {listed}"
        )
    return (
        "Há um campo obrigatório em branco nesta página do formulário. "
        "Qual o texto do campo que você está vendo e qual valor devo usar?"
    )


def forced_missing_data_ask(cfg: dict | None, fields: list[dict]) -> str | None:
    """Primeira pergunta obrigatória: painel ausente ou escala 1–10 na página."""
    asks = list_missing_panel_asks(cfg, fields)
    if asks:
        return asks[0]
    scales = _iter_skill_scale_fields(fields)
    if scales:
        return _ask_for_page_field(scales[0])
    return None


def forced_address_ask_question(cfg: dict | None, fields: list[dict]) -> str | None:
    """Compat: alias de forced_missing_data_ask (rua/CEP e demais campos do painel)."""
    return forced_missing_data_ask(cfg, fields)


def type_should_ask(
    args: dict,
    fields: list[dict],
    cfg: dict | None,
) -> str | None:
    """Se a IA tentou digitar dado sem base no painel (ou placeholder) → ask."""
    from form_rules import looks_like_fake_address

    cfg = cfg or {}
    text = str((args or {}).get("text") or "")
    label = ""
    field = None
    idx = (args or {}).get("field")
    if isinstance(idx, int):
        for f in fields or []:
            if f.get("index") == idx:
                field = f
                label = str(f.get("label") or "")
                break
    if not label:
        label = str((args or {}).get("selector") or "")
    if looks_like_fake_address(text):
        spec = match_panel_field_spec(field, label=label)
        if spec:
            return spec[2]
        return address_ask_question("street")
    spec = match_panel_field_spec(field, label=label)
    if not spec:
        return None
    profile_key, _kind, question = spec
    if not str(cfg.get(profile_key) or "").strip():
        return question
    return None


def address_type_should_ask(
    args: dict,
    fields: list[dict],
    cfg: dict | None,
) -> str | None:
    """Compat: alias de type_should_ask."""
    return type_should_ask(args, fields, cfg)


def classify_address_ask_question(question: str) -> str | None:
    q = question or ""
    for _key, kind, matcher, _ask in _PANEL_FIELD_SPECS:
        if matcher.search(q):
            return kind
    if re.search(r"endere[cç]o|rua\s*e\s*n", q, re.I):
        return "street"
    return None


def classify_panel_ask_question(question: str) -> str | None:
    """profile_key correspondente à pergunta do ask, se houver."""
    q = question or ""
    for profile_key, _kind, matcher, ask_q in _PANEL_FIELD_SPECS:
        if matcher.search(q) or ask_q[:40].casefold() in q.casefold():
            return profile_key
    return None


def persist_panel_ask_answer(
    connect_fn,
    cfg: dict,
    *,
    question: str,
    answer: str,
) -> str | None:
    """Salva resposta no setting do painel (+ cfg em memória). Retorna o key ou None."""
    text = (answer or "").strip()
    key = classify_panel_ask_question(question)
    if not key and classify_address_ask_question(question) == "street":
        key = "candidate_street"
    elif not key and classify_address_ask_question(question) == "postal":
        key = "candidate_postal_code"
    if not key or not text or not callable(connect_fn):
        return None
    cfg[key] = text
    try:
        with connect_fn() as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, text),
            )
    except Exception as exc:
        LOG.warning("copiloto: falha ao salvar %s no perfil: %s", key, exc)
        return key
    LOG.info("copiloto: perfil atualizado %s=%s", key, text[:80])
    return key


def persist_address_answer(
    connect_fn,
    cfg: dict,
    *,
    question: str,
    answer: str,
) -> str | None:
    """Compat: delega a persist_panel_ask_answer."""
    key = persist_panel_ask_answer(connect_fn, cfg, question=question, answer=answer)
    if not key:
        return None
    for profile_key, kind, _m, _q in _PANEL_FIELD_SPECS:
        if profile_key == key:
            return kind
    return key


def fill_panel_fields_from_profile(page, fields: list[dict], cfg: dict) -> list[str]:
    """Após o humano responder, preenche na página os campos casados com o perfil."""
    filled: list[str] = []
    cfg = cfg or {}
    for field in fields or []:
        spec = match_panel_field_spec(field)
        if not spec:
            continue
        profile_key, kind, _q = spec
        value = str(cfg.get(profile_key) or "").strip()
        if not value:
            continue
        sel = field_selector(field)
        tag = str(field.get("tag") or "")
        ok = False
        if tag in {"select", "combobox"}:
            ok = safe_select(page, sel, list(field.get("options") or []), value)
        else:
            ok = safe_fill(page, sel, value)
        if ok:
            filled.append(f"{kind}={value[:40]}")
    return filled


def fill_address_fields_from_profile(page, fields: list[dict], cfg: dict) -> list[str]:
    """Compat: preenche qualquer campo do painel (inclui rua/CEP)."""
    return fill_panel_fields_from_profile(page, fields, cfg)


def missing_panel_block(cfg: dict | None, fields: list[dict]) -> str:
    """Texto para o prompt: o que falta no painel e aparece na página."""
    asks = list_missing_panel_asks(cfg, fields)
    if not asks:
        return "(nenhum campo do painel ausente nesta página)"
    return "MUST ask the human — do NOT invent:\n" + "\n".join(f"- {q}" for q in asks)


# ---------------------------------------------------------------------------
# Partes puras (testaveis sem navegador)
# ---------------------------------------------------------------------------

def _action_vocab(*, allow_submit: bool) -> str:
    base = ACTION_VOCAB
    if not allow_submit:
        return base
    extra = """
FINISH MODE (unknown ATS — no site driver):
- You own the FULL application end-to-end. Keep clicking Next/Continue until the
  final Submit/Apply/Enviar candidatura, then CLICK IT.
- After a successful submit click, emit {"action":"done","reason":"submitted"}.
- Do NOT emit done before submitting. Do not wait for a human unless ask/abort.
""".strip()
    patched = base.replace(
        "then click\n  Next/Continue/Review (never Submit) OR emit `done` so the bot resumes.",
        "then click Next/Continue and finally Submit/Apply yourself (no bot will finish).",
    ).replace(
        "- NEVER emit a click whose target text matches submit/apply/finalizar/enviar. The\n"
        "  caller handles the real submission; you only unblock the flow.",
        "- You MAY click Submit/Apply/Enviar candidatura when the form is complete.\n"
        "  Prefer Next/Continue until that final button appears.",
    ).replace(
        "- When the flow looks unblocked (a Continue/Next is now clickable, required fields\n"
        "  are filled), emit `done`.",
        "- When required fields are filled, advance with Next; when Submit is available,\n"
        "  click it, then emit `done` with reason submitted.",
    )
    return patched + "\n" + extra


def panel_profile_block(cfg: dict | None) -> str:
    """Campos estruturados do painel para o prompt (omite vazios)."""
    cfg = cfg or {}
    lines = []
    for key, label in _PANEL_PROFILE_FIELDS:
        value = str(cfg.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    if any(str(cfg.get(k) or "").strip() for k in ("salary_expectation_brl", "salary_expectation_usd")):
        lines.append(
            "salary_note: panel amounts are MONTHLY mid-level targets; convert FX; "
            "match field period; for intern/junior LOWER unrealistic figures "
            "(may go below panel); for mid+ stay above Brazil floor and slightly "
            "under local market; prefer salary_usd when present"
        )
    return "\n".join(lines)


def copilot_resume_excerpt(
    resume_json: str,
    resume_summary: str = "",
    *,
    budget: int = 2000,
) -> str:
    """Extrato priorizado: location/auth/employer/education antes de skills."""
    budget = max(200, int(budget))
    data = None
    raw = (resume_json or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (json.JSONDecodeError, ValueError, TypeError):
            data = None
    if data is None:
        return _clip(resume_summary or resume_json, budget)

    parts: list[str] = []

    def _add(title: str, value: object, *, item_limit: int | None = None) -> None:
        if isinstance(value, list):
            items = [str(v).strip() for v in value if str(v).strip()]
            if item_limit is not None:
                items = items[:item_limit]
            if items:
                parts.append(title + ":\n- " + "\n- ".join(_clip(i, 220) for i in items))
        else:
            text = str(value or "").strip()
            if text:
                parts.append(f"{title}:\n{_clip(text, 400)}")

    _add("Location notes", data.get("location_notes"))
    _add("Work authorization", data.get("work_authorization_notes"))
    from form_rules import extract_current_employer

    employer = extract_current_employer(data)
    if employer:
        parts.append(f"Current employer:\n{_clip(employer, 120)}")
    _add("Education", data.get("education"), item_limit=8)
    headline = str(data.get("headline") or "").strip()
    seniority = str(data.get("seniority") or "").strip()
    years = str(data.get("years_of_experience") or "").strip()
    if headline or seniority or years:
        bits = [b for b in (headline, f"seniority={seniority}" if seniority else "", f"years={years}" if years else "") if b]
        parts.append("Profile: " + " | ".join(bits))
    skills = data.get("technical_skills") or data.get("skills") or []
    tools = data.get("tools") or []
    skill_items = [str(v).strip() for v in (list(skills) + list(tools)) if str(v).strip()][:12]
    if skill_items:
        parts.append("Skills: " + ", ".join(skill_items))
    experience = data.get("experience")
    if isinstance(experience, list) and experience:
        exp_lines = []
        for item in experience[:3]:
            exp_lines.append(_clip(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False), 280))
        if exp_lines:
            parts.append("Recent experience:\n- " + "\n- ".join(exp_lines))

    text = "\n\n".join(parts).strip()
    if not text:
        return _clip(resume_summary or resume_json, budget)
    return text if len(text) <= budget else text[: budget - 1] + "…"


def extract_action(text: str) -> dict | None:
    """Extrai o PRIMEIRO objeto JSON do texto da IA. None = resposta invalida."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        break
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict) and str(obj.get("action") or "").strip():
        obj.setdefault("args", {})
        return obj
    return None


def expand_action_steps(act: dict) -> list[dict]:
    """Normaliza action única ou batch → lista de passos (máx MAX_STEPS_PER_TURN)."""
    if not act:
        return []
    action = str(act.get("action") or "").strip().lower()
    args = act.get("args") if isinstance(act.get("args"), dict) else {}
    why = str(act.get("reason") or "")
    if action == "batch":
        steps_raw = args.get("steps") if isinstance(args, dict) else None
        if not isinstance(steps_raw, list):
            return []
        # ask/abort/done dentro de batch: promove o primeiro (antes eram descartados).
        for step in steps_raw:
            if not isinstance(step, dict):
                continue
            name = str(step.get("action") or "").strip().lower()
            if name in {"ask", "abort", "done"}:
                step_args = step.get("args") if isinstance(step.get("args"), dict) else {}
                return [{"action": name, "args": step_args, "reason": why or str(step.get("reason") or "")}]
        out: list[dict] = []
        for step in steps_raw[:MAX_STEPS_PER_TURN]:
            if not isinstance(step, dict):
                continue
            name = str(step.get("action") or "").strip().lower()
            if not name or name in {"batch"}:
                continue
            step_args = step.get("args") if isinstance(step.get("args"), dict) else {}
            out.append({"action": name, "args": step_args, "reason": why})
        return out
    return [{"action": action, "args": args, "reason": why}]


def action_signature(action: str, args: dict) -> str:
    """Assinatura estável p/ detectar loops (mesma ação + args chave)."""
    keys = sorted((args or {}).keys())
    parts = [action]
    for k in keys:
        if k in {"text", "code", "question", "reason"}:
            parts.append(f"{k}=…")
        else:
            parts.append(f"{k}={args.get(k)}")
    return "|".join(parts)


def is_no_progress_outcome(action: str, desc: str, *, refused_submit: bool = False) -> bool:
    """True quando o turno não destravou o fluxo (não vale gastar mais retries cegos)."""
    if refused_submit:
        return True
    act = (action or "").casefold()
    if act in {"info", "wait", "mouse", "scroll"}:
        return True
    d = (desc or "").casefold()
    if any(tok in d for tok in ("failed", "no target", "no click", "error:", "unknown action", "invalid")):
        return True
    return False


def should_abort_for_stagnation(no_progress_streak: int, *, limit: int = STAGNATION_LIMIT) -> bool:
    return no_progress_streak >= max(1, int(limit))


def history_has_progress(history: list[str]) -> bool:
    return any(_PROGRESS_HINT_RE.search(h or "") for h in history)


def find_advance_button_index(buttons: list[dict]) -> int | None:
    """Índice de Next/Continue/Review visível, nunca Submit/Apply final."""
    for b in buttons or []:
        if not b.get("visible"):
            continue
        label = f"{b.get('text') or ''} {b.get('aria') or ''}".strip()
        if not label:
            continue
        if target_is_submit(label) or _SUBMIT_CLICK_RE.search(label):
            continue
        if _ADVANCE_RE.search(label):
            try:
                return int(b["index"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def find_submit_button_index(buttons: list[dict]) -> int | None:
    """Índice do botão de envio final (Submit/Apply/Enviar candidatura)."""
    for b in buttons or []:
        if not b.get("visible"):
            continue
        label = f"{b.get('text') or ''} {b.get('aria') or ''}".strip()
        if not label:
            continue
        if _SUBMIT_CLICK_RE.search(label) or target_is_submit(label):
            # evita "Easy Apply" / "Apply on company website" genéricos demais
            if re.search(r"easy\s*apply|company\s+website|external", label, re.I):
                continue
            try:
                return int(b["index"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def try_heuristic_advance(page, buttons: list[dict]) -> tuple[bool, str]:
    """Clica Next/Continue/Review sem gastar turno de IA. Retorna (ok, detalhe)."""
    idx = find_advance_button_index(buttons)
    if idx is None:
        return False, "sem botao de avanço"
    try:
        if click_scanned_button(page, idx):
            return True, f"clicked advance B{idx}"
    except Exception as exc:
        return False, f"advance falhou: {exc}"
    return False, "advance nao clicou"


def try_heuristic_submit(page, buttons: list[dict]) -> tuple[bool, str]:
    """Clica Submit/Apply final (só use com allow_submit)."""
    idx = find_submit_button_index(buttons)
    if idx is None:
        return False, "sem botao de envio"
    try:
        if click_scanned_button(page, idx):
            return True, f"clicked submit B{idx}"
    except Exception as exc:
        return False, f"submit falhou: {exc}"
    return False, "submit nao clicou"


def looks_ready_for_handoff(buttons: list[dict], *, had_progress: bool) -> bool:
    """Se já avançamos campos e há Next/Review, o bot pode retomar sem mais IA."""
    return bool(had_progress and find_advance_button_index(buttons) is not None)


_RESUME_ASK_RE = re.compile(
    r"\b(resume|curr[ií]culo|curriculum|\bcv\b)\b",
    re.I,
)
_FILE_ASK_RE = re.compile(
    r"\b(resume|curr[ií]culo|curriculum|\bcv\b|upload|anexar|arquivo|file|portfolio|"
    r"certificado|certificate|transcript|diploma|attachment|cover\s*letter\s*file)\b",
    re.I,
)


def looks_like_resume_ask(question: str) -> bool:
    return bool(_RESUME_ASK_RE.search(question or ""))


def looks_like_file_ask(question: str) -> bool:
    return bool(_FILE_ASK_RE.search(question or ""))


def file_field_indices(fields: list[dict]) -> list[int]:
    out: list[int] = []
    for f in fields or []:
        if str(f.get("type") or "").casefold() == "file":
            try:
                out.append(int(f["index"]))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def try_upload_file(page, fields: list[dict], file_path: str) -> tuple[bool, str]:
    """Anexa um arquivo em inputs type=file. Retorna (ok, detalhe)."""
    path = (file_path or "").strip()
    if not path:
        return False, "sem file_path"
    import os

    if not os.path.isfile(path):
        return False, f"arquivo inexistente: {path}"
    idxs = file_field_indices(fields)
    if not idxs:
        try:
            loc = page.locator('input[type="file"]')
            if loc.count() <= 0:
                return False, "sem campo file"
            loc.first.set_input_files(path, timeout=8000)
            return True, f"uploaded {os.path.basename(path)} (locator file)"
        except Exception as exc:
            return False, f"upload falhou: {exc}"
    uploaded = 0
    for idx in idxs:
        sel = f'[{MARK_FIELD_ATTR}="{idx}"]'
        try:
            page.locator(sel).first.set_input_files(path, timeout=8000)
            uploaded += 1
        except Exception:
            try:
                page.locator('input[type="file"]').first.set_input_files(path, timeout=8000)
                uploaded += 1
                break
            except Exception:
                continue
    if uploaded:
        return True, f"uploaded {os.path.basename(path)} to {uploaded} field(s)"
    return False, "nao conseguiu set_input_files"


def try_upload_resume(page, fields: list[dict], resume_path: str) -> tuple[bool, str]:
    return try_upload_file(page, fields, resume_path)


def build_copilot_prompt(
    *,
    reason: str,
    facts: str,
    resume_summary: str,
    resume_json: str,
    job: dict,
    url: str,
    fields: list[dict],
    buttons: list[dict],
    page_text: str,
    history: list[str],
    panel_profile: str = "",
    allow_submit: bool = False,
    resume_path: str = "",
    cfg: dict | None = None,
) -> str:
    """Prompt compacto com contexto total + snapshot da pagina + historico."""
    job = job or {}
    field_rows = [
        f"  [F{f['index']}] {f.get('tag')}/{f.get('type') or ''} "
        f"label={_clip(f.get('label'), 60)} name={_clip(f.get('name'), 30)} "
        f"id={_clip(f.get('id'), 30)} ph={_clip(f.get('placeholder'), 30)}"
        + (f" options={f['options'][:12]}" if f.get("options") else "")
        + (" REQUIRED" if f.get("required") else "")
        for f in fields[:40]
    ]
    button_rows = [
        f"  [B{b['index']}] {_clip((b.get('text') or b.get('aria') or '?'), 50)}"
        for b in buttons
        if b.get("visible")
    ][:30]
    hist = "\n".join(f"  - {h}" for h in history[-8:]) or "  (nenhuma ainda)"
    excerpt = copilot_resume_excerpt(resume_json, resume_summary, budget=2000)
    resume_line = (
        f"path={resume_path} (use action upload — do NOT ask the human)"
        if (resume_path or "").strip()
        else "(missing — only then ask)"
    )
    missing = missing_panel_block(cfg, fields)
    return (
        _action_vocab(allow_submit=allow_submit)
        + "\n\n=== STUCK BECAUSE ===\n" + _clip(reason, 300)
        + "\n\n=== RESUME FILE ===\n" + resume_line
        + "\n\n=== CANDIDATE FACTS ===\n" + (_clip(facts, 1200) or "(none)")
        + "\n\n=== PANEL PROFILE ===\n" + (_clip(panel_profile, 800) or "(none)")
        + "\n\n=== MISSING FROM PANEL (ask — never invent) ===\n" + missing
        + "\n\n=== RESUME EXCERPT (prioritized) ===\n" + (excerpt or "(none)")
        + "\n\n=== JOB ===\n"
        + f"title={_clip(job.get('title'), 120)} | company={_clip(job.get('company'), 80)}\n"
        + _clip(job.get("description"), 1200)
        + "\n\n=== CURRENT PAGE ===\nurl: " + _clip(url, 200)
        + "\nFIELDS:\n" + ("\n".join(field_rows) or "  (none)")
        + "\nBUTTONS:\n" + ("\n".join(button_rows) or "  (none)")
        + "\nPAGE TEXT (excerpt):\n" + _clip(page_text, 1400)
        + "\n\n=== ACTIONS SO FAR ===\n" + hist
        + "\n\n=== NEXT ACTION ===\nReply with one JSON object only."
    )


def target_is_submit(label: str) -> bool:
    """Guardrail: botao de envio final nunca e clicado pelo copiloto."""
    return bool(_SUBMIT_BLOCK_RE.search(label or ""))


def click_target_label(args: dict, fields: list[dict], buttons: list[dict]) -> str:
    """Texto real do alvo do clique (p/ guardrail), quando identificavel."""
    if args.get("button") is not None:
        for b in buttons:
            if b.get("index") == args["button"]:
                return f"{b.get('text') or ''} {b.get('aria') or ''}"
    if args.get("field") is not None:
        for f in fields:
            if f.get("index") == args["field"]:
                return f"{f.get('label') or ''} {f.get('name') or ''}"
    return str(args.get("label") or "")


def _clip(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Executor (navegador) - um turno por vez, sobre a pagina "ativa"
# ---------------------------------------------------------------------------

def snapshot_page(page) -> tuple[list[dict], list[dict], str]:
    fields = collect_fields(page)
    buttons = scan_buttons(page)
    try:
        page_text = str(page.evaluate("() => document.body.innerText.slice(0, 4000)"))
    except Exception:
        page_text = ""
    return fields, buttons, page_text


def resolve_selector(args: dict, fields: list[dict]) -> str | None:
    """indice de campo -> seletor estavel; ou CSS livre."""
    idx = args.get("field")
    if isinstance(idx, int):
        for f in fields:
            if f.get("index") == idx:
                return field_selector(f)
        return f'[{MARK_FIELD_ATTR}="{idx}"]'
    sel = args.get("selector")
    return str(sel) if sel else None


def _do_click(page, args: dict, fields: list[dict]) -> str:
    if args.get("button") is not None:
        click_scanned_button(page, int(args["button"]))
        return "clicked button #" + str(args["button"])
    if args.get("field") is not None:
        sel = resolve_selector(args, fields)
        if not sel:
            return "no target"
        page.locator(sel).first.click(timeout=5000)
        return "clicked field #" + str(args.get("field"))
    if args.get("x") is not None and args.get("y") is not None:
        page.mouse.click(int(args["x"]), int(args["y"]))
        return f"clicked coords ({args['x']},{args['y']})"
    sel = args.get("selector")
    if sel:
        page.locator(str(sel)).first.click(timeout=5000)
        return "clicked selector"
    return "no click target given"


def _do_check(page, args: dict, fields: list[dict]) -> str:
    sel = resolve_selector(args, fields)
    if not sel:
        return "no target"
    want = bool(args.get("checked", True))
    # clique via JS funciona em inputs opacidade-0 (React custom como InHire)
    result = page.evaluate(
        "(sel, want) => {"
        " const el = document.querySelector(sel);"
        " if (!el) return 'missing';"
        " const isOn = !!(el.checked || el.getAttribute('aria-checked') === 'true'"
        " || el.getAttribute('data-state') === 'checked' || el.classList.contains('checked'));"
        " if (isOn !== want) { el.click(); return 'toggled'; }"
        " return 'already'; }",
        sel, want,
    )
    return f"check:{result}"


def exec_action(
    page,
    context,
    action: str,
    args: dict,
    fields: list[dict],
    *,
    resume_path: str = "",
) -> tuple[str, object]:
    """Roda UM comando. Retorna (descricao, nova_page_ou_None)."""
    try:
        if action == "click":
            return _do_click(page, args, fields), None
        if action == "type":
            sel = resolve_selector(args, fields)
            if not sel:
                return "no target", None
            text = str(args.get("text", ""))
            from form_rules import looks_like_fake_address

            if looks_like_fake_address(text):
                return "blocked placeholder address — use ask", None
            ok = safe_fill(page, sel, text)
            return ("typed" if ok else "type failed"), None
        if action == "select":
            sel = resolve_selector(args, fields)
            opts: list[str] = []
            idx = args.get("field")
            if isinstance(idx, int):
                for f in fields:
                    if f.get("index") == idx:
                        opts = f.get("options") or []
            ok = safe_select(page, sel, opts, str(args.get("value", "")))
            return ("selected" if ok else "select failed"), None
        if action == "upload":
            path = str(args.get("path") or resume_path or "").strip()
            ok, detail = try_upload_file(page, fields, path)
            return detail, None
        if action == "check":
            return _do_check(page, args, fields), None
        if action == "mouse":
            page.mouse.move(int(args.get("x", 0)), int(args.get("y", 0)))
            return "mouse moved", None
        if action == "press":
            page.keyboard.press(str(args.get("key", "Tab")))
            return "pressed " + str(args.get("key")), None
        if action == "scroll":
            page.mouse.wheel(0, int(args.get("y", 0)))
            return "scrolled", None
        if action == "wait":
            time.sleep(min(5.0, max(0.1, float(args.get("seconds", 1)))))
            return "waited", None
        if action == "open":
            url = str(args.get("url", "")).strip()
            if not url.lower().startswith("http"):
                return "invalid url", None
            new = context.new_page()
            new.goto(url, wait_until="domcontentloaded", timeout=45000)
            return "opened " + url[:80], new
        if action == "close":
            others = [p for p in list(getattr(context, "pages", []) or []) if p is not page]
            page.close()
            return "closed page", (others[-1] if others else None)
        if action == "js":
            page.evaluate(str(args.get("code", "(() => {})")))
            return "ran js", None
        if action == "info":
            return "snapshot requested", None
        return f"unknown action: {action}", None
    except Exception as exc:
        LOG.debug("copilot exec %s falhou: %s", action, exc)
        return f"error: {exc}", None


# ---------------------------------------------------------------------------
# Retry de rate-limit (preserva history/facts no mesmo takeover)
# ---------------------------------------------------------------------------

def call_ai_with_rate_limit_retry(
    *,
    call_fn,
    prompt: str,
    provider: str,
    model: str,
    api_key: str,
    history: list[str],
    turn: int,
    connect_fn=None,
    open_ask_id: int | None = None,
    budget_seconds: float = AI_RATE_LIMIT_BUDGET_SECONDS,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
    backoff_fn=None,
) -> str:
    """Chama a IA; em 429/quota espera e repete sem limpar o contexto.

    Auth (401/403 sem rate-limit) propaga AiUnavailableError na hora.
    """
    from job_queue import backoff_seconds, is_rate_limit_error

    if backoff_fn is None:
        backoff_fn = backoff_seconds

    deadline = monotonic_fn() + max(1.0, float(budget_seconds))
    attempt = 0
    while True:
        try:
            return call_fn(
                prompt=prompt,
                provider=provider,
                model=model,
                api_key=api_key,
                max_output_tokens=700,
                temperature=0.1,
            )
        except AiUnavailableError as exc:
            if not is_rate_limit_error(exc):
                raise
            remaining = deadline - monotonic_fn()
            if remaining <= 1:
                raise
            attempt += 1
            delay = int(backoff_fn(attempt, rate_limited=True))
            delay = max(1, min(delay, int(remaining)))
            history.append(f"turn {turn}: rate-limit, retry {attempt} em ~{delay}s")
            LOG.warning("copiloto: rate-limit (tentativa %s), espera %ss", attempt, delay)
            if open_ask_id is not None and callable(connect_fn):
                from copilot_asks import set_ask_hint

                set_ask_hint(
                    connect_fn,
                    open_ask_id,
                    f"Rate limit — nova tentativa em ~{delay}s",
                )
            sleep_fn(delay)


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def copilot_takeover(page, context, *, reason: str, cfg: dict, ai: dict,
                     max_turns: int = MAX_TURNS) -> tuple[str, str, object]:
    """Override de IA. Retorna (estado, detalhe, pagina_ativa).

    estado: SOLVED | SUBMITTED | ABORTED | UNAVAILABLE.
    Com ``ai['allow_submit']=True`` (site sem driver) o copiloto pode clicar
    Submit e concluir a candidatura; sem isso, só desbloqueia e devolve ao bot.
    """
    provider = str(ai.get("provider") or "")
    model = str(ai.get("model") or "")
    api_key = str(ai.get("api_key") or "")
    if not (provider and model and api_key):
        return UNAVAILABLE, "copiloto indisponivel (sem IA configurada)", page

    allow_submit = bool(ai.get("allow_submit"))
    resume_path = str(ai.get("resume_path") or "").strip()
    facts = str(ai.get("facts") or "")
    job = ai.get("job") or {}
    resume_summary = str(ai.get("resume_summary") or "")
    resume_json = str(ai.get("resume_json") or "")
    cfg = dict(cfg or {})  # mutável: asks salvam rua/CEP/telefone etc. no perfil
    history: list[str] = []
    active = page
    open_ask_id: int | None = None
    connect_fn = ai.get("connect_fn")
    no_progress_streak = 0
    last_sig = ""
    resume_uploaded = False

    from ai_client import call_ai_text

    def _fail_open_ask() -> None:
        nonlocal open_ask_id
        if open_ask_id is not None and callable(connect_fn):
            from copilot_asks import fail_ask

            fail_ask(connect_fn, open_ask_id)
            open_ask_id = None

    def _confirm_submit_claim(detail: str) -> tuple[str, str, object] | None:
        """Só SUBMITTED com texto de sucesso real; clique sozinho não basta."""
        from wait_human import wait_for_success_signal

        if wait_for_success_signal(active, timeout_s=20):
            LOG.info("copiloto: envio confirmado na pagina — %s", detail)
            return SUBMITTED, f"copiloto enviou a candidatura ({detail})", active
        LOG.warning("copiloto: clique de envio sem confirmacao de sucesso — %s", detail)
        return None

    def _maybe_finish(buttons_now: list[dict], *, tag: str) -> tuple[str, str, object] | None:
        """Em modo allow_submit: tenta Submit e só confirma com sucesso na página."""
        if not allow_submit:
            return None
        ok, detail = try_heuristic_submit(active, buttons_now)
        if not ok:
            return None
        history.append(f"{tag}: heuristic {detail}")
        confirmed = _confirm_submit_claim(detail)
        if confirmed:
            return confirmed
        history.append(f"{tag}: submit sem confirmacao de sucesso")
        return None

    for turn in range(max_turns):
        try:
            fields, buttons, page_text = snapshot_page(active)
            try:
                url = active.url or ""
            except Exception:
                url = ""
            # Currículo do painel: sobe sozinho em campos file (não pergunta ao humano).
            if resume_path and not resume_uploaded and file_field_indices(fields):
                ok, detail = try_upload_resume(active, fields, resume_path)
                if ok:
                    resume_uploaded = True
                    history.append(f"turn {turn}: auto {detail}")
                    LOG.info("copiloto: %s", history[-1])
                    no_progress_streak = 0
                    time.sleep(random.uniform(*_TURN_PAUSE))
                    fields, buttons, page_text = snapshot_page(active)
            # Dado do painel ausente + campo na página → pergunta no painel (sem chamar a IA).
            forced_ask = forced_missing_data_ask(cfg, fields)
            if forced_ask:
                act = {
                    "action": "ask",
                    "args": {"question": forced_ask},
                    "reason": "dado ausente no painel — obrigatório perguntar",
                }
                raw = json.dumps(act, ensure_ascii=False)
            else:
                # Site com driver: handoff cedo. Site desconhecido: tenta Submit, senão segue.
                if looks_ready_for_handoff(buttons, had_progress=history_has_progress(history)):
                    if allow_submit:
                        finished = _maybe_finish(buttons, tag=f"turn {turn}")
                        if finished:
                            return finished
                        ok, detail = try_heuristic_advance(active, buttons)
                        if ok:
                            history.append(f"turn {turn}: heuristic {detail}")
                            LOG.info("copiloto: %s", history[-1])
                            no_progress_streak = 0
                            time.sleep(random.uniform(*_TURN_PAUSE))
                            continue
                    else:
                        ok, detail = try_heuristic_advance(active, buttons)
                        if ok:
                            history.append(f"turn {turn}: heuristic {detail}")
                            LOG.info("copiloto: avanço heurístico + handoff — %s", detail)
                        else:
                            LOG.info("copiloto: handoff ao bot (página pronta) — %s", detail)
                        return SOLVED, f"destravado (handoff; {detail})", active
                prompt = build_copilot_prompt(
                    reason=reason, facts=facts, resume_summary=resume_summary,
                    resume_json=resume_json, job=job, url=url, fields=fields,
                    buttons=buttons, page_text=page_text, history=history,
                    panel_profile=panel_profile_block(cfg),
                    allow_submit=allow_submit,
                    resume_path=resume_path,
                    cfg=cfg,
                )
                raw = call_ai_with_rate_limit_retry(
                    call_fn=call_ai_text,
                    prompt=prompt,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    history=history,
                    turn=turn,
                    connect_fn=connect_fn if callable(connect_fn) else None,
                    open_ask_id=open_ask_id,
                )
        except AiUnavailableError as exc:
            _fail_open_ask()
            return abort_close(active, context, f"IA indisponivel no meio do fluxo: {exc}")
        except Exception as exc:
            LOG.debug("copilot turn %d erro: %s", turn, exc)
            _fail_open_ask()
            return abort_close(active, context, f"erro do copiloto: {exc}")

        if open_ask_id is not None and callable(connect_fn):
            from copilot_asks import complete_ask

            complete_ask(connect_fn, open_ask_id)
            open_ask_id = None

        act = extract_action(raw)
        if not act:
            history.append(f"turn {turn}: resposta irregular, pedindo JSON")
            no_progress_streak += 1
            if should_abort_for_stagnation(no_progress_streak):
                _fail_open_ask()
                return abort_leave_open(
                    active, "copiloto estagnou (respostas irregulares)"
                )
            continue

        action = str(act["action"]).strip().lower()
        args = act.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        why = str(act.get("reason") or "")[:160]

        # Batch com ask/abort/done: promove (antes o expand descartava ask).
        if action == "batch":
            steps_raw = args.get("steps") if isinstance(args.get("steps"), list) else []
            for step in steps_raw:
                if not isinstance(step, dict):
                    continue
                name = str(step.get("action") or "").strip().lower()
                if name in {"ask", "abort", "done"}:
                    action = name
                    step_args = step.get("args") if isinstance(step.get("args"), dict) else {}
                    args = step_args
                    why = str(step.get("reason") or why)[:160]
                    break

        # type com dado ausente/placeholder → vira ask (não deixa inventar).
        if action not in {"ask", "abort", "done"}:
            for step in expand_action_steps(act):
                if str(step.get("action") or "").strip().lower() != "type":
                    continue
                step_args = step.get("args") if isinstance(step.get("args"), dict) else {}
                ask_q = type_should_ask(step_args, fields, cfg)
                if ask_q:
                    history.append(
                        f"turn {turn}: type bloqueado (dado ausente/inventado) → ask no painel"
                    )
                    LOG.info("copiloto: %s", history[-1])
                    action = "ask"
                    args = {"question": ask_q}
                    why = "dado ausente no painel — obrigatório perguntar"
                    break

        if action == "done":
            if allow_submit and "submit" in why.casefold():
                confirmed = _confirm_submit_claim(why)
                if confirmed:
                    return confirmed
                LOG.info("copiloto: done/submit sem confirmacao — %s", why)
            if allow_submit:
                # IA disse done sem submit — tenta enviar antes de aceitar handoff vazio.
                try:
                    _f, buttons_now, _t = snapshot_page(active)
                except Exception:
                    buttons_now = buttons
                finished = _maybe_finish(buttons_now, tag=f"turn {turn}")
                if finished:
                    return finished
                ok, detail = try_heuristic_advance(active, buttons_now)
                if ok:
                    history.append(f"turn {turn}: heuristic {detail} (pos-done)")
                    continue
            LOG.info("copiloto: destravou apos %d turno(s) - %s", turn + 1, why)
            return SOLVED, f"destravado pelo copiloto ({why})", active
        if action == "abort":
            return abort_close(active, context, why or "a IA nao conseguiu automatizar")
        if action == "ask":
            question = str(args.get("question") or why or "").strip()
            question = clarify_ask_question(question, fields, cfg)
            if not question:
                history.append(f"turn {turn}: ask sem pergunta")
                no_progress_streak += 1
                if should_abort_for_stagnation(no_progress_streak):
                    return abort_leave_open(active, "copiloto estagnou (ask vazio)")
                continue
            # Currículo já no painel → sobe o PDF, não pergunta.
            if looks_like_resume_ask(question) and resume_path:
                ok, detail = try_upload_resume(active, fields, resume_path)
                history.append(f"turn {turn}: resume ask → {detail}")
                LOG.info("copiloto: %s", history[-1])
                if ok:
                    resume_uploaded = True
                    no_progress_streak = 0
                    continue
            if not callable(connect_fn):
                return abort_close(active, context, "ask sem connect_fn — " + question[:120])
            from copilot_asks import (
                KIND_FILE,
                STATUS_AWAITING_AI,
                STATUS_ANSWERED,
                STATUS_CANCELLED,
                create_ask,
                get_ask,
                load_facts,
                wait_for_answer,
            )

            job_id = None
            try:
                job_id = int((job or {}).get("id")) if (job or {}).get("id") is not None else None
            except (TypeError, ValueError):
                job_id = None
            now = str(ai.get("now_iso") or "")
            ask_kind = KIND_FILE if looks_like_file_ask(question) else "text"
            try:
                ask_id = create_ask(
                    connect_fn,
                    job_id=job_id,
                    question=question,
                    now_iso=now or "now",
                    kind=ask_kind,
                )
            except Exception as exc:
                return abort_close(active, context, f"falha ao criar ask: {exc}")
            wait_min = 12
            try:
                wait_min = int(str(cfg.get("linkedin_human_wait_minutes") or "12"))
            except ValueError:
                wait_min = 12
            wait_min = max(3, min(45, wait_min))
            LOG.info(
                "copiloto: perguntando no painel (ask #%s kind=%s): %s",
                ask_id, ask_kind, question[:120],
            )
            progress = ai.get("progress")
            if callable(progress):
                try:
                    progress(f"perguntando no painel: {question[:140]}")
                except Exception:
                    pass
            status, answer = wait_for_answer(connect_fn, ask_id, minutes=wait_min)
            if status not in {STATUS_AWAITING_AI, STATUS_ANSWERED}:
                label = "cancelado" if status == STATUS_CANCELLED else "timeout"
                return abort_close(
                    active, context, f"humano {label} a pergunta do copiloto: {question[:120]}"
                )
            row = get_ask(connect_fn, ask_id) or {}
            human_file = str(row.get("file_path") or "").strip()
            if human_file:
                ok, detail = try_upload_file(active, fields, human_file)
                history.append(f"turn {turn}: ask file → {detail}")
                LOG.info("copiloto: %s", history[-1])
                if ok:
                    no_progress_streak = 0
            lang = str((job or {}).get("language") or "en")
            facts = load_facts(connect_fn, lang) or (facts + f"\n{question}: {answer}").strip()
            persist_panel_ask_answer(connect_fn, cfg, question=question, answer=str(answer or ""))
            try:
                filled_now = fill_panel_fields_from_profile(active, fields, cfg)
                if filled_now:
                    history.append(f"turn {turn}: perfil → preenchido ({', '.join(filled_now)})")
                    LOG.info("copiloto: %s", history[-1])
            except Exception as exc:
                LOG.debug("copiloto: fill pós-ask falhou: %s", exc)
            history.append(f"turn {turn}: ask -> answered ({_clip(answer, 80)})")
            # Não mantém o modal aberto durante a próxima chamada à IA
            # (rate-limit pode segurar awaiting_ai por vários minutos).
            open_ask_id = None
            no_progress_streak = 0
            continue

        steps = expand_action_steps(act)
        if not steps:
            history.append(f"turn {turn}: batch vazio")
            no_progress_streak += 1
            if should_abort_for_stagnation(no_progress_streak):
                return abort_leave_open(active, "copiloto estagnou (batch vazio)")
            continue

        turn_progressed = False
        clicked_submit = False
        for step in steps:
            step_action = str(step.get("action") or "").strip().lower()
            step_args = step.get("args") if isinstance(step.get("args"), dict) else {}
            step_why = str(step.get("reason") or why)[:160]
            label = click_target_label(step_args, fields, buttons)
            if step_action == "click" and target_is_submit(label) and not allow_submit:
                history.append(
                    f"turn {turn}: recusado clique em botao de envio ({step_why})"
                )
                no_progress_streak += 1
                continue
            sig = action_signature(step_action, step_args)
            if sig and sig == last_sig:
                history.append(f"turn {turn}: acao repetida ignorada ({sig})")
                no_progress_streak += 1
                continue
            desc, new_page = exec_action(
                active, context, step_action, step_args, fields, resume_path=resume_path
            )
            if new_page is not None:
                active = new_page
            history.append(
                f"turn {turn}: {step_action}({json.dumps(step_args, ensure_ascii=False)[:120]}) "
                f"-> {desc} | {step_why}"
            )
            LOG.info("copiloto: %s", history[-1])
            if "uploaded" in (desc or "").casefold():
                resume_uploaded = True
            if (
                allow_submit
                and step_action == "click"
                and (target_is_submit(label) or _SUBMIT_CLICK_RE.search(label or ""))
                and "clicked" in (desc or "").casefold()
            ):
                clicked_submit = True
            if is_no_progress_outcome(step_action, desc):
                no_progress_streak += 1
            else:
                no_progress_streak = 0
                turn_progressed = True
            last_sig = sig
            time.sleep(random.uniform(*_TURN_PAUSE))

        if clicked_submit:
            confirmed = _confirm_submit_claim("click submit")
            if confirmed:
                return confirmed
            history.append(f"turn {turn}: submit clicado sem texto de sucesso")
            # Continua o loop — talvez precise captcha/validação; não mente "enviado".

        if turn_progressed:
            try:
                _fields2, buttons2, _text2 = snapshot_page(active)
            except Exception:
                buttons2 = buttons
            if allow_submit:
                finished = _maybe_finish(buttons2, tag=f"turn {turn}")
                if finished:
                    return finished
            ok, detail = try_heuristic_advance(active, buttons2)
            if ok:
                history.append(f"turn {turn}: heuristic {detail}")
                LOG.info("copiloto: %s", history[-1])
                no_progress_streak = 0
                time.sleep(random.uniform(*_TURN_PAUSE))

        if not turn_progressed and should_abort_for_stagnation(no_progress_streak):
            _fail_open_ask()
            return abort_leave_open(
                active, "copiloto estagnou sem progresso — abortando cedo"
            )

    # Esgotou turnos
    if history_has_progress(history):
        try:
            _f, buttons_end, _t = snapshot_page(active)
            if allow_submit:
                finished = _maybe_finish(buttons_end, tag="final")
                if finished:
                    return finished
            ok, detail = try_heuristic_advance(active, buttons_end)
            if ok:
                history.append(f"final: heuristic {detail}")
                LOG.info("copiloto: %s", history[-1])
                if allow_submit:
                    try:
                        _f2, buttons2, _t2 = snapshot_page(active)
                        finished = _maybe_finish(buttons2, tag="final2")
                        if finished:
                            return finished
                    except Exception:
                        pass
            else:
                detail = detail or "sem advance"
        except Exception:
            detail = "snapshot final falhou"
        if allow_submit:
            # Sem bot para retomar — deixa Chrome aberto p/ revisão/envio.
            _fail_open_ask()
            return abort_leave_open(
                active,
                f"site sem driver: esgotou turnos sem concluir o envio ({detail})",
            )
        LOG.info("copiloto: turnos esgotados com progresso — handoff ao bot (%s)", detail)
        return SOLVED, f"copiloto parcial com progresso; bot retoma ({detail})", active

    _fail_open_ask()
    return abort_leave_open(active, "copiloto esgotou os turnos sem destravar")


def abort_leave_open(page, why: str) -> tuple[str, str, object]:
    """Abort sem fechar o Chrome — o fluxo assistido/revisão assume a página."""
    return ABORTED, f"{COPILOT_FAIL_PREFIX} nao foi possivel automatizar o fluxo - {why[:200]}", page


def abort_close(page, context, why: str) -> tuple[str, str, object]:
    """Fecha tudo e devolve a nota amarela com o motivo."""
    try:
        for p in list(getattr(context, "pages", []) or []):
            try:
                p.close()
            except Exception:
                pass
    except Exception:
        pass
    if page is not None:
        try:
            if not page.is_closed():
                page.close()
        except Exception:
            pass
    return ABORTED, f"{COPILOT_FAIL_PREFIX} nao foi possivel automatizar o fluxo - {why[:200]}", None


__all__ = [
    "ABORTED",
    "ACTION_VOCAB",
    "AI_RATE_LIMIT_BUDGET_SECONDS",
    "COPILOT_FAIL_PREFIX",
    "MAX_STEPS_PER_TURN",
    "MAX_TURNS",
    "SOLVED",
    "STAGNATION_LIMIT",
    "SUBMITTED",
    "UNAVAILABLE",
    "abort_close",
    "abort_leave_open",
    "action_signature",
    "build_copilot_prompt",
    "call_ai_with_rate_limit_retry",
    "click_target_label",
    "copilot_resume_excerpt",
    "copilot_takeover",
    "exec_action",
    "expand_action_steps",
    "extract_action",
    "find_advance_button_index",
    "find_submit_button_index",
    "history_has_progress",
    "is_no_progress_outcome",
    "looks_ready_for_handoff",
    "panel_profile_block",
    "resolve_selector",
    "should_abort_for_stagnation",
    "snapshot_page",
    "target_is_submit",
    "try_heuristic_advance",
    "try_heuristic_submit",
]
