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
    r"disab|defici|pcd|orienta\w*\s+sexual|sexual\s+orienta|lgbt|veteran|ind[ií]gena|self.?identified",
    re.I,
)
# consentimento legal — nunca responder automaticamente aqui
CONSENT_RE = re.compile(
    r"\bagree\b|consent|terms|privacy|autorizo|concordo|pol[ií]tica de privacidade",
    re.I,
)

NOT_INFORMED = "not_informed"

# aliases de cada valor canônico do perfil → texto que casa com opções reais
_PROFILE_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "gender": {
        "male": ("male", "man", "homem", "masculino"),
        "female": ("female", "woman", "mulher", "feminino"),
        "other": ("other", "non-binary", "nao binario", "não binário", "outro"),
        NOT_INFORMED: ("prefer not", "nao informar", "não informar", "decline", "prefiro nao"),
    },
    "race": {
        "white": ("white", "branca", "branco"),
        "black": ("black", "preta", "preto", "afro"),
        "pardo": ("pardo", "mixed", "multirracial", "morena", "moreno"),
        "asian": ("asian", "asiatica", "asiático", "amarela"),
        "indigenous": ("indigenous", "indigena", "indígena", "amarela? nao", "branca? nao"),
        "not_specified": ("not specified", "nao informado"),
        NOT_INFORMED: ("prefer not", "nao informar", "não informar", "decline"),
    },
    "lgbtq": {
        "yes": ("lgbt", "yes", "sim"),
        "no": ("straight", "hetero", "no", "nao", "não"),
        NOT_INFORMED: ("prefer not", "nao informar", "não informar", "decline"),
    },
    "pcd": {
        "yes": ("yes", "sim", "pcd", "deficiency", "disability"),
        "no": ("no", "nao", "não"),
        NOT_INFORMED: ("prefer not", "nao informar", "não informar"),
    },
}

_CATEGORY_FOR_RE = (
    ("gender", re.compile(r"gender|sex\b|sexo", re.I)),
    ("race", re.compile(r"race|ethnic|racial|ra[cç]a|cor\b|ind[ií]gena", re.I)),
    ("lgbtq", re.compile(r"lgbt|orienta|sexual orientation|gay|trans", re.I)),
    ("pcd", re.compile(r"disab|defici|pcd|accommodat", re.I)),
)


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


def profile_for_diversity(cfg: dict[str, str], category: str) -> list[str] | None:
    """Opções aceitas p/ a categoria, vindo do painel. None = sem perfil → humano."""
    setting_map = {
        "gender": "candidate_gender",
        "race": "candidate_race",
        "lgbtq": "candidate_lgbtq",
        "pcd": "candidate_pcd",
    }
    if category not in setting_map:
        return None
    raw = (cfg.get(setting_map[category]) or cfg.get(f"inhire_{category}") or NOT_INFORMED).strip().casefold()
    aliases = _PROFILE_ALIASES.get(category, {}).get(raw)
    if aliases is None:  # valor livre do painel: usa direto como consulta
        aliases = (raw,)
    return list(aliases)


def _truthy_pcd(cfg: dict[str, str]) -> str:
    raw = (cfg.get("candidate_pcd") or cfg.get("inhire_pcd") or "0").strip().casefold()
    if raw in {"1", "yes", "sim", "true"}:
        return "yes"
    if raw in {"0", "no", "nao", "não", "false"}:
        return "no"
    return raw or NOT_INFORMED


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
  const seen = new Map();
  const nodes = scope.querySelectorAll('input[type="radio"], input[type="checkbox"]');
  let idx = 0;
  for (const el of nodes) {
    if (el.disabled || el.hidden) continue;
    const rect = el.getBoundingClientRect();
    if (!rect.width && !rect.height && !el.closest('label')) continue;
    const id = el.id || '';
    let label = '';
    if (id) {
      const lab = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (lab) label = (lab.innerText || '').trim();
    }
    if (!label && el.closest('label')) label = (el.closest('label').innerText || '').trim();
    if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');
    if (!label) label = (el.value || '').trim();
    let legend = '';
    const fs = el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg) legend = (lg.innerText || '').trim();
    }
    if (!legend) {
      const rg = el.closest('[role="radiogroup"], [role="group"]');
      if (rg) {
        const by = rg.getAttribute('aria-labelledby');
        const node = by ? document.getElementById(by) : null;
        legend = node ? (node.innerText || '').trim() : (rg.getAttribute('aria-label') || '');
      }
    }
    const key = legend || el.name || label || 'group';
    if (!seen.has(key)) {
      seen.set(key, groups.length);
      groups.push({ question: (key || '').slice(0, 300), name: el.name || '', multiple: el.type === 'checkbox', options: [] });
    }
    const g = groups[seen.get(key)];
    el.setAttribute('data-radar-choice', String(idx));
    g.options.push({ label: (label || '').slice(0, 200), index: idx });
    idx += 1;
  }
  return groups;
}"""

CLEAN_CHOICE_ATTR_JS = """() => {
  document.querySelectorAll('[data-radar-choice]').forEach((el) => el.removeAttribute('data-radar-choice'));
}"""


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
    pcd_state = _truthy_pcd(ctx.cfg)
    for group in groups:
        question = group.get("question") or ""
        options = [o.get("label") or "" for o in group.get("options") or []]
        kind = classify_group(f"{question} {' '.join(options)}")
        if kind == "consent":
            continue  # obrigaçao do handler, nao decisao de IA
        chosen: list[str] | None = None
        if kind.startswith("diversity:"):
            category = kind.split(":", 1)[1]
            if category == "pcd":
                aliases = _PROFILE_ALIASES["pcd"].get(pcd_state)
            else:
                aliases = profile_for_diversity(ctx.cfg, category)
            if aliases:
                for alias in aliases:
                    match = pick_select_option(options, alias)
                    if match:
                        chosen = [match]
                        break
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
                    try:
                        scope.locator(f'[data-radar-choice="{opt["index"]}"]').first.check(force=True, timeout=4000)
                        done += 1
                    except Exception as exc:
                        LOG.debug("%s check(%s) falhou: %s", log_prefix, opt_label, exc)
        if done:
            answered.append(f"{question[:80]} → {', '.join(chosen)[:120]}")
        else:
            deferred.append(question[:120])
    try:
        scope.evaluate(CLEAN_CHOICE_ATTR_JS)
    except Exception:
        pass
    return answered, deferred
