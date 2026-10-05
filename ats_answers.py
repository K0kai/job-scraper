"""Respostas inteligentes para perguntas de formulário de candidatura.

Três camadas, na ordem:
1. **Diversidade** (gênero/raça/PCD/...) — NUNCA por IA; vem do perfil do
   candidato no painel, casada com a opção mais parecida. "não informar" é
   resposta legítima.
2. **Perguntas da empresa com opções** (radio/checkbox) — IA recebe TODAS as
   opções + a análise do currículo já feita, devolve o texto EXATO da(s)
   opção(ões); insegura → deixa para o humano.
3. **Cache global** — pergunta normalizada (sem acentos/pontuação/caixa) →
   resposta; pergunta parecida em vaga futura reaproveita sem nova chamada.

Consentimento/EULA NÃO passa por aqui (é obrigação do handler, com regex
CONSENT própria) — marcar termo legal por IA seria assinar pela pessoa.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Callable

from form_rules import normalize, pick_select_option
from resume_pipeline import AiUnavailableError

LOG = logging.getLogger("job-scraper")

ConnectFn = Callable[[], sqlite3.Connection]

# perguntas que são identidade — perfil humano decide, jamais IA
DIVERSITY_RE = re.compile(
    r"gender|sex\b|sexo|race|ethnic|racial|ra[cç]a|cor\b|"
    r"disab|defici|pcd|orienta\w*\s+sexual|sexual\s+orienta|lgbt|veteran|ind[ií]gena|self.?identified|"
    r"accessib|accommodat|necessit[ao].{0,30}(adapt|recurs)|\bICD\b|\bCID\b|laudo|m[eé]dico",
    re.I,
)
#: identidade em area de texto que pede NUMERO/codigo (ex.: CID do laudo) —
#: nunca template canonico; so nota livre do painel ou humano
NUMBERISH_RE = re.compile(r"number|n[uú]mero|c[oó]digo|code|qual o", re.I)
#: radio/select de tipo de contratacao (Contractor/Employee, PJ/CLT) — decisao do
#: candidato no painel, nunca IA e nunca chute
CONTRACT_RE = re.compile(
    r"\bcontract\s*type\b|type of contract|contract type|employment type|"
    r"tipo de contrata|modelo de contrata|regime de contrata|\bcontrata[cç][aã]o\b",
    re.I,
)
_CONTRACT_TOKENS = {
    "employee": ("employee", "empregado", "clt", "efetivo", "celetista", "full time"),
    "contractor": ("contractor", "contract", "pj", "autonomo", "prestador", "freelance", "independent"),
}
# consentimento legal — nunca responder automaticamente aqui
CONSENT_RE = re.compile(
    r"\bagree\b|consent|terms|privacy|autorizo|concordo|pol[ií]tica de privacidade",
    re.I,
)
SKILL_RATING_RE = re.compile(
    r"escala.{0,25}1.{0,8}10|scale.{0,25}1.{0,8}10|"
    r"rate.{0,40}(experience|skill|proficiency|your)|"
    r"avalie.{0,40}(experi|conhec|habilidade)|"
    r"nível.{0,20}1.{0,8}10|n[ií]vel.{0,20}1.{0,8}10|"
    r"how would you rate|qu[aã]o.{0,20}(experi|conhec|habilidade)|"
    r"self.?assess|autoavalia",
    re.I,
)

NOT_INFORMED = "not_informed"

#: pergunta de identidade que PEDS explicacao — template humano, nunca texto canonico
DESCRIBE_RE = re.compile(r"describ|descrev|explai|explic|tell us|why|por que|detail", re.I)

#: valor canonico do painel → texto a digitar em textarea/input de identidade
_DIVERSITY_TEXT: dict[str, dict[str, tuple[str, str]]] = {
    "gender": {
        "female": ("Female", "Mulher"),
        "male": ("Male", "Homem"),
        "other": ("Non-binary", "Não binário"),
        NOT_INFORMED: ("Prefer not to disclose", "Prefiro não informar"),
    },
    "race": {
        "white": ("White", "Branca"),
        "black": ("Black", "Preta"),
        "pardo": ("Mixed race", "Parda"),
        "asian": ("Asian", "Amarela/asiática"),
        "indigenous": ("Indigenous", "Indígena"),
        NOT_INFORMED: ("Prefer not to disclose", "Prefiro não informar"),
    },
    "pcd": {
        "yes": ("Yes", "Sim"),
        "no": ("No", "Não"),
        NOT_INFORMED: ("Prefer not to disclose", "Prefiro não informar"),
    },
    "lgbtq": {
        "yes": ("Yes", "Sim"),
        "no": ("No", "Não"),
        NOT_INFORMED: ("Prefer not to disclose", "Prefiro não informar"),
    },
}

_DIVERSITY_SETTING = {
    "gender": "candidate_gender",
    "race": "candidate_race",
    "lgbtq": "candidate_lgbtq",
    "pcd": "candidate_pcd",
}


def diversity_text_for(cfg: dict[str, str], category: str, language: str = "en") -> str | None:
    """Texto a DIGITAR em area de texto de identidade. Nunca e texto de IA.

    'other'/categoria sem mapa usa a nota livre do painel se houver.
    not_informed tem frase canonica honesta ('Prefiro nao informar').
    """
    if category not in _DIVERSITY_SETTING:  # veteran/outros: so nota livre
        return (cfg.get("candidate_diversity_note") or "").strip() or None
    raw = (
        _truthy_pcd(cfg)
        if category == "pcd"
        else (cfg.get(_DIVERSITY_SETTING[category]) or NOT_INFORMED).strip().casefold()
    )
    if raw == "other":
        note = (cfg.get("candidate_diversity_note") or "").strip()
        if note:
            return note
    entry = _DIVERSITY_TEXT.get(category, {}).get(raw or NOT_INFORMED)
    if entry is None:  # valor livre digitado no painel → usar direto
        return raw or None
    return entry[1] if (language or "en") == "pt" else entry[0]


# aliases de cada valor canônico do perfil → texto que casa com opções reais
# (ordem importa: aliases mais específicos/seguros primeiro — "man" antes de
# "male", que casaria "female" por substring)
_PROFILE_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "gender": {
        "male": ("man", "male", "homem", "masculino"),
        "female": ("woman", "female", "mulher", "feminino"),
        "other": ("non-binary", "nao binario", "não binário", "other", "outro"),
        NOT_INFORMED: ("rather not", "prefer not", "nao informar", "não informar", "decline", "prefiro nao"),
    },
    "race": {
        "white": ("white", "branca", "branco"),
        "black": ("black", "preta", "preto", "afro"),
        "pardo": ("brown", "pardo", "mixed race", "mixed", "multirracial", "morena", "moreno"),
        "asian": ("yellow", "asian", "asiatica", "asiática", "amarela"),
        "indigenous": ("indigenous", "indigena", "indígena"),
        "not_specified": ("not specified", "nao informado"),
        NOT_INFORMED: ("rather not", "prefer not", "nao informar", "não informar", "decline"),
    },
    "lgbtq": {
        "yes": ("lgbt", "lesbian", "gay", "bisexual", "pansexual", "asexual", "yes", "sim"),
        "no": ("heterosexual", "straight", "hetero", "no", "nao", "não"),
        "gay": ("homosexual", "gay"),
        "lesbian": ("lesbian", "homosexual"),
        "bisexual": ("bisexual"),
        "pansexual": ("pansexual"),
        "asexual": ("asexual"),
        "other": ("other", "outro"),
        NOT_INFORMED: ("rather not", "prefer not", "nao informar", "não informar", "decline"),
    },
    "pcd": {
        "yes": ("yes", "sim", "pcd", "deficiency", "disability"),
        "no": ("no", "nao", "não"),
        NOT_INFORMED: ("rather not", "prefer not", "nao informar", "não informar"),
    },
}

#: opções que significam "não declarado" — jamais chutadas se o perfil é afirmativo
NEUTRAL_OPT_RE = re.compile(r"rather not|prefer not|prefiro nao|prefiro não|don'?t belong", re.I)

_CATEGORY_FOR_RE = (
    ("gender", re.compile(r"gender|sex\b|sexo", re.I)),
    ("race", re.compile(r"race|ethnic|racial|ra[cç]a|cor\b|ind[ií]gena", re.I)),
    ("lgbtq", re.compile(r"lgbt|orienta|sexual orientation|gay|trans", re.I)),
    ("pcd", re.compile(r"disab|defici|pcd|accommodat", re.I)),
)


#: opcoes que significam "recusa declarada" — nunca chutadas se o perfil e afirmativo
_NOT_DECLARED_RE = re.compile(r"rather not|prefer not|nao informar|não informar|decline", re.I)


def profile_state(cfg: dict[str, str], category: str) -> str:
    """Valor canonico do painel para a categoria (pcd normalizado yes/no)."""
    if category == "pcd":
        return _truthy_pcd(cfg)
    setting = _DIVERSITY_SETTING.get(category)
    if not setting:
        return NOT_INFORMED
    return (cfg.get(setting) or NOT_INFORMED).strip().casefold()


def _match_option(options: list[str], aliases: tuple[str, ...], *, skip: re.Pattern | None = None) -> str | None:
    """Primeira opcao real que casa um alias — por TOKENS, nunca substring solta
    ('man' nao pode casar 'Woman...', 'black' nao pode casar 'Brown ... black features')."""
    for alias in aliases:
        na = normalize(alias)
        if not na:
            continue
        na_tokens = set(na.split())
        for opt in options:
            if skip and skip.search(opt):
                continue
            nopt = normalize(opt)
            if nopt == na or nopt.startswith(na) or na_tokens <= set(nopt.split()):
                return opt
    return None


def diversity_answer_for_options(cfg: dict[str, str], category: str, options: list[str]) -> str | None:
    """Opcao real que casa o perfil de diversidade para uma categoria. None = humano."""
    if category not in _PROFILE_ALIASES:
        return None
    value = profile_state(cfg, category)
    aliases = _PROFILE_ALIASES[category].get(value, (value,) if value else ())
    skip = None if value == NOT_INFORMED else _NOT_DECLARED_RE
    return _match_option(options, aliases, skip=skip)


#: grupo tipo InHire: "Voce pertence a um dos grupos abaixo?" (checkbox multiplas)
GROUPS_Q_RE = re.compile(r"belong to one of.{0,25}groups|pertence.{0,25}(aos? )?grupos", re.I | re.S)


def _infer_category_from_options(options: list[str]) -> str | None:
    """Sem pergunta clara: deduz a categoria pelo conteudo das opcoes."""
    hay = " | ".join(options).casefold()
    scores = {
        "race": len(re.findall(r"black person|brown person|indigenous|preta|parda|ind[ií]gena", hay)),
        "pcd": len(re.findall(r"disabilit|deficienc|pcd", hay)),
        "lgbtq": len(re.findall(r"lgbt|lesbian|gay|bisexual|pansexual|asexual|hetero", hay)),
        "gender": len(re.findall(r"cisgender|transgender|non-binary|agender|gender identity", hay)),
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 2 else None

#: (categoria, valor do perfil) → palavras-chave que identificam a opcao no grupo
_MEMBERSHIP_KEYWORDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("race", "black"): ("black person", "pretos?"),
    ("race", "pardo"): ("brown person", "pardos?"),
    ("race", "indigenous"): ("indigenous", "indigena"),
    ("gender", "female"): ("woman", "mulher"),
    ("lgbtq", "yes"): ("lgbt",),
    ("pcd", "yes"): ("with disabilities", "com deficiencia"),
}


def match_membership_options(cfg: dict[str, str], options: list[str]) -> list[str] | None:
    """Marcacoes do grupo de 'pertence a algum grupo' a partir do perfil. None = humano."""
    states = {c: profile_state(cfg, c) for c in ("gender", "race", "lgbtq", "pcd")}
    chosen: list[str] = []
    for (cat, val), keywords in _MEMBERSHIP_KEYWORDS.items():
        if states.get(cat) != val:
            continue
        for kw in keywords:
            match = next((opt for opt in options if normalize(kw) in normalize(opt)), None)
            if match and match not in chosen:
                chosen.append(match)
                break
    if chosen:
        return chosen

    def find(sub: str) -> str | None:
        return next((opt for opt in options if sub in normalize(opt)), None)

    any_informed = any(v != NOT_INFORMED for v in states.values())
    all_informed = all(v != NOT_INFORMED for v in states.values())
    if not any_informed:  # tudo "prefiro nao informar" → recusa explicita se existir
        got = find("rather not") or find("prefer not") or find("nao informar")
        return [got] if got else None
    if all_informed:  # afirmou tudo e nenhum casa grupo minorizado → "nenhum dos grupos"
        got = find("don t belong") or find("not belong") or find("none") or find("nenhum")
        return [got] if got else None
    return None  # mistura de informado + nao informado: ambiguo, humano decide


def classify_group(question: str) -> str:
    """'diversity:<categoria>' | 'consent' | 'company'."""
    if CONSENT_RE.search(question):
        return "consent"
    if DIVERSITY_RE.search(question):
        for category, rex in _CATEGORY_FOR_RE:
            if rex.search(question):
                return f"diversity:{category}"
        return "diversity:other"
    return "company"




def _truthy_pcd(cfg: dict[str, str]) -> str:
    """
    Retorna "yes" se candidato declarou PCD, "no" se não, ou valor informado se diferente.
    """
    raw = (cfg.get("candidate_pcd") or "").strip().casefold()
    if raw in {"1", "yes", "sim", "true"}:
        return "yes"
    if raw in {"0", "no", "nao", "não", "false"}:
        return "no"
    if not raw:
        return NOT_INFORMED
    return raw


def contract_choice_for(cfg: dict[str, str], options: list[str]) -> str | None:
    """Opcao Contractor/Employee conforme preferencia do painel. 'ask'/vazio → None (humano)."""
    pref = (cfg.get("candidate_contract_type") or "").strip().casefold()
    if pref in {"", "ask", "both", "nao_informado", "not_informed"}:
        return None
    tokens = _CONTRACT_TOKENS.get(pref)
    if not tokens:  # valor livre: casa por substring direto
        tokens = (pref,)
    return _match_option(options, tokens)


def build_choices_prompt(
    *,
    question: str,
    options: list[str],
    multiple: bool,
    job: dict,
    resume_summary: str,
    resume_json: str,
    facts: str,
) -> str:
    language = job.get("language") or "en"
    language_name = "Portuguese" if language == "pt" else "English"
    options_block = "\n".join(f"- {opt}" for opt in options)
    limit = "up to all that apply" if multiple else "EXACTLY ONE"
    return f"""Answer this job-application question in {language_name} by choosing from the given options.
Choose {limit}. Use ONLY the resume analysis and candidate facts. Never invent anything.
Return the chosen option TEXTS verbatim, one per line, and nothing else.
If the facts are insufficient to choose, reply with exactly: CANNOT_ANSWER

Question: {question}
Options:
{options_block}

Candidate facts: {facts or '[none]'}
Resume summary: {resume_summary or '[none]'}
Resume analysis JSON: {resume_json[:8000] or '[none]'}
Job title: {job.get('title')}
Company: {job.get('company')}
"""


def label_looks_like_skill_rating(label: str) -> bool:
    hay = label or ""
    if SKILL_RATING_RE.search(hay):
        return True
    low = hay.casefold()
    if re.search(r"1\s*[-–]\s*10|1\s+a\s+10|\bscale\b|\bescala\b", low):
        return any(
            tok in low
            for tok in (
                "experi",
                "skill",
                "habilidade",
                "conhecimento",
                "proficiency",
                "familiar",
                "domínio",
                "dominio",
                "anos de",
                "years of",
            )
        )
    return False


def options_look_like_numeric_scale(options: list[str]) -> bool:
    if len(options) < 3:
        return False
    nums: list[int] = []
    for raw in options:
        m = re.match(r"^\s*(\d{1,2})\s*$", str(raw).strip())
        if not m:
            return False
        nums.append(int(m.group(1)))
    return min(nums) >= 1 and max(nums) <= 10 and len(set(nums)) >= 3


def build_skill_rating_prompt(
    *,
    question: str,
    options: list[str],
    job: dict,
    resume_summary: str,
    resume_json: str,
    facts: str,
) -> str:
    opts = ", ".join(options)
    return f"""Pick ONE self-rating option for this job application skill question.
Use ONLY the resume analysis and candidate facts. Be honest and conservative:
- strong daily use / core skill → 8–9
- solid but not primary → 6–7
- beginner / exposure only → 3–5
- no evidence → reply exactly: CANNOT_ANSWER
Return ONLY the exact option text from this list: {opts}

Question: {question}
Candidate facts: {facts or '[none]'}
Resume summary: {resume_summary or '[none]'}
Resume analysis JSON: {resume_json[:6000] or '[none]'}
Job title: {job.get('title')}
Company: {job.get('company')}
"""


def choose_skill_rating_option(question: str, options: list[str], ai: dict) -> str | None:
    """Uma opção da escala 1–10 com cache."""
    if not options or not ai.get("api_key"):
        return None
    cached = cache_get(ai.get("connect_fn"), question)
    if cached and cached.strip().upper() != "CANNOT_ANSWER":
        pick = parse_choices_answer(cached, options, multiple=False)
        if pick:
            return pick[0]
    try:
        from ai_client import call_ai_text

        prompt = build_skill_rating_prompt(
            question=question,
            options=options,
            job=ai.get("job") or {},
            resume_summary=ai.get("resume_summary") or "",
            resume_json=ai.get("resume_json") or "",
            facts=ai.get("facts") or "",
        )
        text = call_ai_text(
            prompt=prompt,
            provider=ai.get("provider") or "",
            model=ai.get("model") or "",
            api_key=ai.get("api_key") or "",
        )
        if not text or "CANNOT_ANSWER" in text.upper():
            return None
        pick = parse_choices_answer(text, options, multiple=False)
        if pick:
            cache_put(
                ai.get("connect_fn"),
                question=question,
                kind="skill_rating",
                answer=text.strip()[:4000],
                provider=ai.get("provider") or "",
                model=ai.get("model") or "",
                now_iso=ai.get("now_iso") or "",
            )
            return pick[0]
    except (AiUnavailableError, ValueError, RuntimeError) as exc:
        LOG.info("skill rating IA indisponível (%s): %s", question[:60], exc)
    return None


def answer_skill_rating_fields(scope, ctx, *, log_prefix: str = "form") -> tuple[list[str], list[str]]:
    """Preenche selects/inputs numéricos de autoavaliação 1–10 (fora de radio groups)."""
    from ats_kernel import field_selector, safe_fill, safe_select

    answered: list[str] = []
    deferred: list[str] = []
    ai = ctx.ai or {}
    try:
        from ats_kernel import collect_fields

        fields = collect_fields(scope)
    except Exception:
        return answered, deferred
    for field in fields:
        label = str(field.get("label") or "")
        options = [str(o) for o in (field.get("options") or []) if str(o).strip()]
        tag = str(field.get("tag") or "")
        ftype = str(field.get("type") or "").casefold()
        is_scale_select = tag in {"select", "combobox"} and options_look_like_numeric_scale(options)
        is_scale_number = tag == "input" and ftype == "number"
        if not (is_scale_select or is_scale_number):
            continue
        if not label_looks_like_skill_rating(label):
            continue
        opts = options if options else [str(n) for n in range(1, 11)]
        chosen = choose_skill_rating_option(label, opts, ai)
        if not chosen:
            deferred.append(label[:120])
            continue
        sel = field_selector(field)
        ok = False
        if is_scale_select:
            ok = safe_select(scope, sel, options, chosen)
        else:
            ok = safe_fill(scope, sel, chosen)
        if ok:
            answered.append(f"{label[:80]} → {chosen}")
        else:
            deferred.append(label[:120])
    return answered, deferred


def parse_choices_answer(text: str, options: list[str], *, multiple: bool) -> list[str] | None:
    """Linhas da IA → textos EXATOS das opções reais; insegura → None (humano)."""
    if not text or "CANNOT_ANSWER" in text.upper():
        return None
    by_norm: dict[str, str] = {}
    for opt in options:
        by_norm.setdefault(normalize(opt), opt)
    chosen: list[str] = []
    for line in text.splitlines():
        cand = line.strip().lstrip("-•* ").strip()
        if not cand:
            continue
        if cand.upper() == "CANNOT_ANSWER":
            return None
        exact = by_norm.get(normalize(cand))
        if exact is None:
            # fuzzy conservador: so aceita contencoes claras entre as strings
            # normalizadas (evita casar "10 years" com "1-3 years" por tokens).
            cand_norm = normalize(cand)
            for opt_norm, opt in by_norm.items():
                if len(cand_norm) >= 4 and (cand_norm in opt_norm or opt_norm in cand_norm):
                    exact = opt
                    break
        if exact is not None and exact not in chosen:
            chosen.append(exact)
        if not multiple and chosen:
            break
    return chosen or None


# ---------------------------------------------------------------------------
# Cache global por pergunta normalizada
# ---------------------------------------------------------------------------


def cache_get(connect_fn: ConnectFn | None, question: str) -> str | None:
    if connect_fn is None:
        return None
    key = normalize(question)
    try:
        with connect_fn() as db:
            row = db.execute(
                "SELECT answer FROM ai_answer_cache WHERE question_norm=?", (key,)
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE ai_answer_cache SET hits=hits+1 WHERE question_norm=?", (key,)
                )
                return str(row["answer"])
    except sqlite3.Error as exc:
        LOG.debug("cache_get falhou: %s", exc)
    return None


def cache_put(
    connect_fn: ConnectFn | None,
    *,
    question: str,
    kind: str,
    answer: str,
    provider: str = "",
    model: str = "",
    now_iso: str = "",
) -> None:
    if connect_fn is None or not answer:
        return
    key = normalize(question)
    try:
        with connect_fn() as db:
            db.execute(
                """INSERT INTO ai_answer_cache(question_norm,question_raw,kind,answer,provider,model,hits,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,0,?,?)
                   ON CONFLICT(question_norm) DO UPDATE SET
                     question_raw=excluded.question_raw, kind=excluded.kind,
                     answer=excluded.answer, provider=excluded.provider,
                     model=excluded.model, updated_at=excluded.updated_at""",
                (key, question[:500], kind, answer[:4000], provider, model, now_iso, now_iso),
            )
    except sqlite3.Error as exc:
        LOG.debug("cache_put falhou: %s", exc)


def ask_open_cached(
    *,
    question: str,
    generate: Callable[[], str],
    connect_fn: ConnectFn | None,
    provider: str = "",
    model: str = "",
    now_iso: str = "",
) -> str:
    """Pergunta aberta com cache global — só chama `generate` em miss."""
    cached = cache_get(connect_fn, question)
    if cached:
        return cached
    answer = generate()
    cache_put(
        connect_fn, question=question, kind="open", answer=answer,
        provider=provider, model=model, now_iso=now_iso,
    )
    return answer


# ---------------------------------------------------------------------------
# Varredura de grupos radio/checkbox no DOM
# ---------------------------------------------------------------------------

_CHOICE_GROUPS_JS = """(root) => {
  const scope = root || document;
  const groups = [];
  const nodes = scope.querySelectorAll('input[type="radio"], input[type="checkbox"]');
  const optLabel = (el) => {
    const id = el.id || '';
    if (id) {
      const lab = (el.getRootNode() || document).querySelector(`label[for="${CSS.escape(id)}"]`);
      if (lab && (lab.innerText || '').trim()) return lab.innerText.trim();
    }
    if (el.closest('label')) return (el.closest('label').innerText || '').trim();
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    return (el.value || '').trim();
  };
  // name compartilhado so agrupa se divide inputs; senao fieldset/grupo/texto.
  const nameCount = {};
  for (const el of nodes) nameCount[el.name] = (nameCount[el.name] || 0) + 1;
  let contSeq = 0;
  const groupKey = (el) => {
    if (el.name && nameCount[el.name] > 1) return 'name:' + el.name;
    const fs = el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg && (lg.innerText || '').trim()) return 'text:' + lg.innerText.trim();
    }
    const rg = el.closest('[role="radiogroup"], [role="group"]');
    if (rg) {
      const by = rg.getAttribute('aria-labelledby');
      const node = by ? (rg.getRootNode() || document).getElementById(by) : null;
      const t = node ? (node.innerText || '').trim() : (rg.getAttribute('aria-label') || '');
      if (t) return 'text:' + t;
    }
    // InHire: name e UUID por input e sem fieldset — sobe ate o menor container
    // que reuna 2+ inputs. Input solo (consentimento) vira grupo pelo proprio label.
    let cur = el.parentElement;
    while (cur && cur.querySelectorAll('input[type="radio"], input[type="checkbox"]').length < 2) {
      cur = cur.parentElement;
    }
    if (!cur) return 'label:' + (optLabel(el) || el.id || '');
    if (!cur.__radarKey) cur.__radarKey = 'cont:' + (contSeq++);
    return cur.__radarKey;
  };
  const questionOf = (key, list) => {
    if (key.startsWith('name:') || key.startsWith('text:') || key.startsWith('label:')) {
      return key.slice(key.indexOf(':') + 1);
    }
    let cont = null;
    for (const el of nodes) {
      let cur = el.parentElement;
      while (cur && cur.querySelectorAll('input[type="radio"], input[type="checkbox"]').length < 2) cur = cur.parentElement;
      if (cur && cur.__radarKey === key) { cont = cur; break; }
    }
    if (!cont) return '';
    // a pergunta pode estar num irmao ANTES do container das opcoes: sobe ate 4 niveis
    for (let up = 0; up < 4 && cont; up++) {
      const all = (cont.innerText || '').trim();
      let cut = all.length;
      for (const e of list) {
        const probe = (e.label || '').slice(0, 40);
        const at = probe ? all.indexOf(probe) : -1;
        if (at >= 0 && at < cut) cut = at;
      }
      const lines = all.slice(0, cut).trim().split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean);
      for (let j = lines.length - 1; j >= 0; j--) {
        if (lines[j].replace(/[*\\s]+$/, '').endsWith('?')) return lines[j];
      }
      cont = cont.parentElement;
    }
    return '';
  };
  const entries = [];
  for (const el of nodes) {
    if (el.disabled || el.hidden) continue;
    const rect = el.getBoundingClientRect();
    if (!rect.width && !rect.height && !el.closest('label')) continue;
    entries.push({ el, key: groupKey(el), label: optLabel(el) || (el.value || '').trim() });
  }
  const byKey = new Map();
  for (const e of entries) {
    if (!byKey.has(e.key)) byKey.set(e.key, []);
    byKey.get(e.key).push(e);
  }
  for (const [key, list] of byKey) {
    const q = questionOf(key, list) || (list[0].label || 'group');
    groups.push({ question: (q || '').slice(0, 300), name: list[0].el.name || '', multiple: list[0].el.type === 'checkbox', options: [] });
  }
  const keyIndex = new Map([...byKey.keys()].map((k, i) => [k, i]));
  let idx = 0;
  for (const e of entries) {
    e.el.setAttribute('data-radar-choice', String(idx));
    groups[keyIndex.get(e.key)].options.push({ label: (e.label || '').slice(0, 200), index: idx });
    idx += 1;
  }
  return groups;
}"""

CLEAN_CHOICE_ATTR_JS = """() => {
  document.querySelectorAll('[data-radar-choice]').forEach((el) => el.removeAttribute('data-radar-choice'));
}"""

_MARK_OPTION_JS = """(idx) => {
  const el = document.querySelector(`[data-radar-choice="${idx}"]`);
  if (!el) return 'missing';
  if (el.checked) return 'already';
  // checkbox custom (input oculto por CSS): clicar o LABEL e nao o input.
  const id = el.id || '';
  const lab = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : el.closest('label');
  try {
    if (lab) lab.click(); else el.click();
  } catch (e) { return 'error'; }
  // radios React podem atualizar assincrono — checa de novo em microtask
  return el.checked ? 'ok' : 'pending';
}"""


def _mark_option(scope, index: int) -> bool:
    """Marca radio/checkbox mesmo com input oculto (clique no label). Nunca lança."""
    try:
        result = scope.evaluate(_MARK_OPTION_JS, index)
    except Exception as exc:
        LOG.debug("mark(%s) evaluate falhou: %s", index, exc)
        return False
    if result in ("already", "ok"):
        return True
    if result == "pending":
        # estado ainda pode estar assincrono — tenta o locator .check force depois
        try:
            scope.locator(f'[data-radar-choice="{index}"]').first.check(force=True, timeout=3000)
            return True
        except Exception:
            return True  # label clicado; React pode ter aceitado sem refletir no input
    return False


def collect_choice_groups(scope) -> list[dict]:
    """Grupos de radio/checkbox (label da opção + legend do grupo). scope = page ou locator.

    Em locator (ex.: modal do Easy Apply), a varredura é RESTITA àquele subtree —
    senão o wizard veria os radios da página por trás do modal.
    """
    try:
        if hasattr(scope, "element_handle"):  # Locator → restringe ao elemento
            handle = scope.element_handle()
            return list(scope.evaluate(_CHOICE_GROUPS_JS, handle) or [])
        return list(scope.evaluate(_CHOICE_GROUPS_JS, None) or [])
    except Exception:
        return []


def answer_choice_groups(scope, ctx, *, log_prefix: str = "form") -> tuple[list[str], list[str]]:
    """Responde grupos: diversidade→perfil, empresa→IA com cache. Nunca lança.

    Retorna (respondidas, deixadas_para_humano).
    """
    answered: list[str] = []
    deferred: list[str] = []
    groups = collect_choice_groups(scope)
    ai = ctx.ai or {}
    for group in groups:
        question = group.get("question") or ""
        options = [o.get("label") or "" for o in group.get("options") or []]
        kind = classify_group(f"{question} {' '.join(options)}")
        if kind == "consent":
            continue  # obrigaçao do handler, nao decisao de IA
        chosen: list[str] | None = None
        # tipo de contratação: decisao do painel (Contractor/Employee). Entra na
        # cadeia antes da IA — sem preferencia definida fica com o humano.
        is_contract = not group.get("multiple") and CONTRACT_RE.search(
            f"{question} {' '.join(options)}"
        )
        # "pertence a um dos grupos?" casa RACA+PCD+LGBTI+ na MESMA pergunta —
        # o texto nao necessariamente cai em DIVERSITY_RE, entao vem primeiro.
        if is_contract:
            got = contract_choice_for(ctx.cfg, options)
            chosen = [got] if got else None
        elif group.get("multiple") and (
            GROUPS_Q_RE.search(question)
            or GROUPS_Q_RE.search(" ".join(options))
        ):
            chosen = match_membership_options(ctx.cfg, options)
        elif kind.startswith("diversity:"):
            category = kind.split(":", 1)[1]
            if category == "other":
                category = _infer_category_from_options(options) or "other"
            got = diversity_answer_for_options(ctx.cfg, category, options)
            chosen = [got] if got else None
        elif (
            not group.get("multiple")
            and options_look_like_numeric_scale(options)
            and label_looks_like_skill_rating(question)
            and ai.get("api_key")
        ):
            got = choose_skill_rating_option(question, options, ai)
            chosen = [got] if got else None
        elif kind == "company" and ai.get("api_key"):
            cached = cache_get(ai.get("connect_fn"), question)
            if cached:
                chosen = parse_choices_answer(cached, options, multiple=bool(group.get("multiple")))
            else:
                try:
                    from ai_client import call_ai_text

                    prompt = build_choices_prompt(
                        question=question,
                        options=options,
                        multiple=bool(group.get("multiple")),
                        job=ai.get("job") or {},
                        resume_summary=ai.get("resume_summary") or "",
                        resume_json=ai.get("resume_json") or "",
                        facts=ai.get("facts") or "",
                    )
                    text = call_ai_text(
                        prompt=prompt,
                        provider=ai.get("provider") or "",
                        model=ai.get("model") or "",
                        api_key=ai.get("api_key") or "",
                    )
                    chosen = parse_choices_answer(text, options, multiple=bool(group.get("multiple")))
                    if chosen:
                        cache_put(
                            ai.get("connect_fn"), question=question, kind="choices",
                            answer=text.strip()[:4000], provider=ai.get("provider") or "",
                            model=ai.get("model") or "", now_iso=ai.get("now_iso") or "",
                        )
                except (AiUnavailableError, ValueError, RuntimeError) as exc:
                    LOG.info("%s IA de opcoes indisponivel (%s): %s", log_prefix, question[:60], exc)
                    if isinstance(exc, AiUnavailableError):
                        ai = {}  # para de tentar IA nos grupos seguintes
        if not chosen:
            deferred.append(question[:120])
            continue
        done = 0
        for opt_label in chosen:
            for opt in group.get("options") or []:
                if (opt.get("label") or "") == opt_label:
                    if _mark_option(scope, int(opt["index"])):
                        done += 1
                    else:
                        LOG.debug("%s check(%s) falhou", log_prefix, opt_label)
        if done:
            answered.append(f"{question[:80]} → {', '.join(chosen)[:120]}")
        else:
            deferred.append(question[:120])
    try:
        scope.evaluate(CLEAN_CHOICE_ATTR_JS)
    except Exception:
        pass
    return answered, deferred
