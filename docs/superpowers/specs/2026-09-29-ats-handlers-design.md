# Design: Arquitetura plugável de handlers ATS

**Data:** 2026-09-29
**Status:** aprovado (brainstorming, 4 seções validadas)

## Objetivo

Permitir que cada página de candidatura externa (ATS) para a qual o LinkedIn ou os
scrapers redirecionam o robô tenha um **driver próprio**, escrito contra uma interface
única e extensível. Hoje só InHire existe, com a lógica hardcoded em ~11 pontos de
`linkedin_apply.py`/`app.py`. Esta spec define:

1. `ats_base.py` — classe base + registro (`@register`, `find_handler`);
2. `ats_kernel.py` — kernel genérico de preenchimento (reuses `form_rules`) com
   salvaguardas de foco/popup;
3. `ats_router.py` — roteador único que substitui os checks `is_inhire_url`;
4. `ats_template.py` + `docs/ats-handler-guide.md` — template copiável e passo a passo
   que qualquer agente segue para adicionar um site novo;
5. Handlers concretos: refactor do **InHire** + novos **Greenhouse**, **Lever**, **Gupy**
   (Workday fica para depois, por ser o mais hostil).

## Decisões do brainstorming

| Tema | Decisão |
|---|---|
| Autonomia | Robô **auto-envia** quando o site não tem captcha/obstáculo e o handler declara `auto_submit_capable=True`; senão cai em modo assistido |
| Sites da 1a entrega | Greenhouse, Lever, Gupy (Workday depois) |
| Estratégia de fill | **Híbrida**: kernel genérico (rótulo→`form_rules`) + hooks específicos por site |
| Template | Guia Markdown passo a passo + módulo `ats_template.py` comentado para copiar |
| Site sem handler | **Não tenta nada** — vaga volta para "Vale a pena olhar" com nota em vermelho `NO_HANDLER: ...` |
| Arquitetura de registro | Opção A: classe base + `@register` (ABC + registry validado em import) |
| Popup/desfoque | Salvaguarda **obrigatória do kernel** (re-âncora de página, fecha intrusos, recobra campo detached) — não substituível pelo handler |

## Fora de escopo

- Auto-submit no **wizard LinkedIn Easy Apply** (política anti-ban: usuário sempre
  confirma o envio final lá). O roteador não toca nesse fluxo.
- Handler Workday (mesma interface, entrega futura).
- Resolver captcha ou bypass de login — jamais. `detect_obstacles` só *classifica*.
- Mudanças em `form_rules` (o kernel consome as regras como estão).

## Arquitetura

```
linkedin_apply.py ──► ats_router.find_handler(url) ──► BaseATSHandler (concreto)
                            │                             │
                            │                             ├── pre_fill / post_fill / advance_step (hooks)
                            │                             ▼
                            └── run_ats_flow ──────► ats_kernel (genérico, com salvaguardas)
                                                          │
                                                          ▼
                                                      form_rules (match rótulo→valor)
```

### `ats_base.py` — contrato do handler

```python
@dataclass
class ApplyContext:
    cfg: dict[str, str]          # settings do painel (candidate_name, ...)
    rules: list[dict]            # form_rules já carregadas
    resume_path: str
    cover_letter: str
    salary: str                  # já resolvido via _salary_from_rules

class FillResult:  ok: bool; filled: list[str]; missing: list[str]; error: str | None
class SubmitResult: clicked: bool; button_label: str; error: str | None

class BaseATSHandler(ABC):
    name: str                          # identificador em logs/UI ("greenhouse")
    hosts: tuple[str, ...]             # hosts que o site atende (sem "www.")
    auto_submit_capable: bool = False  # default seguro: assisted-first

    @classmethod
    def can_handle(cls, url: str) -> bool: ...   # pronto na base: casa netloc (±www, ±porta)
    def wait_ready(self, page) -> bool: ...      # polling ≤15s; False → fill não roda
    def detect_obstacles(self, page) -> list[str]: ...  # ["captcha","login",...]
    def fill(self, page, ctx) -> FillResult: ... # default: delega ao kernel
    def submit(self, page) -> SubmitResult: ...  # default: kernel acha o botão de envio
    def verify_submitted(self, page) -> str: ... # "submitted" | "unknown"

    # hooks opcionais que os concretos sobrescrevem (default = no-op True):
    def pre_fill(self, page, ctx) -> None: ...
    def post_fill(self, page, ctx) -> list[str]: ...   # retorna campos extra preenchidos
    def advance_step(self, page) -> bool: ...          # wizards multi-step custom
```

Registro:

- `register(cls)` — decorador; valida `name` único, `hosts` não colidindo com handler
  já registrado (colisão = `RuntimeError` no import, falha cedo e alto);
- `HANDLERS: list[BaseATSHandler]` — ordem de registro;
- `find_handler(url) -> BaseATSHandler | None`;
- Cada `ats_<site>.py` é importado pelo `ats_router` (lista explícita de imports no
  topo do router — sem mágica de filesystem scan; o guia manda adicionar 1 linha aqui).

### `ats_kernel.py` — preenchimento genérico + salvaguardas de foco

Pipeline executado pelo `fill()` default (e disponível como peça para handlers):

1. **Re-âncora de página** — antes de cada lote de campos, checar `context.pages` e
   manter como página ativa aquela cujo URL ainda casa com `handler.can_handle`. Popup
   que abriu aba nova não faz o robô digitar na janela errada.
2. **Fecha intrusos** — um único `page.evaluate` (estilo `scan_buttons` do
   `browser_engine`) varre overlays conhecidos: cookie banners
   (`#onetrust-accept-btn-handler`, `[aria-label="Accept"]`, "Aceitar"/"Accept"), botões
   `[aria-label="Close"]` de modais de chat. **Nunca** clicaria label de submit
   (lista negra `submit|apply|enviar|candidatar` excluída da varredura).
3. **`safe_fill(page, selector, value)`** — `scroll_into_view` → `click` → `fill`;
   em falha (elemento detached por modal no meio do caminho): fecha intrusos,
   re-localiza o campo por label, 1 retry. Segunda falha → registra em `missing`.
4. **Match de campos** — coleta `input/textarea/select` visíveis + labels (mesma
   heurística de `_collect_controls` de `linkedin_apply.py`), casa label com
   `form_rules.find_rule_for_label` + `resolve_rule_value`; `mode=file` →
   `set_input_files(resume_path)`; `mode=cover_letter` → `fill(cover_letter)`.
5. **Avanço de passo** — só clica "Next/Continuar/Avançar" depois que todos os
   obrigatórios do passo atual estão preenchidos; sem avanço em ≤3 tentativas,
   para e reporta o campo que travou. Wizard multi-step custom usa o hook
   `advance_step` do handler.
6. **Validação pós-fill** — re-escaneia e confirma valor por campo; divergência
   (React re-render limpou o input) vai para `missing`.

O kernel **nunca lança exceção crua** para o roteador — tudo vira
`FillResult(ok, filled, missing, error)`. Página fechada por popup =
`error="pagina fechada"` e segue o resto do fluxo de falha.

### `ats_router.py` — decisão de rota e outcomes

```python
class AtsOutcome: str-enum  # SUBMITTED | ASSISTED | TIMEOUT | FAILED | NO_HANDLER
```

`run_ats_flow(page, context, cfg, rules, resume_path, cover_letter, human_wait)`:

1. Re-âncora `page` em `context.pages` pelo `find_handler` (até ~10s de polling,
   substituindo o loop hardcoded atual de `linkedin_apply._handle_external_ats`);
2. **Sem handler** → `NO_HANDLER` (não preenche, não espera humano — ver UI abaixo);
3. Handler → `wait_ready` → `fill` → `detect_obstacles`:
   - obstacles vazio **e** `auto_submit_capable=True` → `submit` → `verify_submitted`
     (re-verifica por até ~20s) → `SUBMITTED` (auto-envio real);
   - obstacles (captcha/login) **ou** `auto_submit_capable=False` → **assistido**:
     `wait_for_human(page, minutes)` genérico (promovido do `wait_for_human_inhire`,
     que hoje atende sites não-InHire por engano) → `ASSISTED`/`TIMEOUT`;
4. `page.is_closed()` em qualquer ponto → `FAILED` com detalhe.

`linkedin_apply.py` mantém apenas o wizard LinkedIn; `_handle_external_ats` vira um
fino wrapper que chama `run_ats_flow` e mapeia `AtsOutcome` para as closures
`finish_ok`/`block` existentes. Os ~11 pontos de `is_inhire_url` somem
(`app.py:628` e `app.py:893` passam a usar `find_handler(url) is not None`).

### Fallback sem handler → "Vale a pena olhar" com nota vermelha

- O outcome `NO_HANDLER` grava:
  `notes = "NO_HANDLER: apply externo em <host> — nenhum driver ATS instalado. Preencha/envie manualmente se quiser."`
  e status `worth` (via `mark_worth_looking`, como já existe);
- Em `worth_html` (`app.py:2197`), quando `notes.startswith("NO_HANDLER:")` o
  parágrafo da nota sai com `style="color:#b3261e;font-weight:600"` (vermelho) —
  sem CSS novo obrigatório, inline basta;
- O botão "Easy Apply" do card continua existindo: o usuário pode re-tentar depois
  de instalarmos um handler para aquele host.

### Handlers concretos da 1a entrega

| Handler | quirks mapeados (hooks) | auto_submit_capable |
|---|---|---|
| `ats_inhire.py` (refactor) | dropdown React (`post_fill`), telefone +55 antes do número, PCD/diversidade, i18n PT/EN, checkbox privacidade | **False** (captcha no final — como hoje) |
| `ats_greenhouse.py` | hosts `boards.greenhouse.io`, `job-boards.greenhouse.io`; EULA checkbox; upload resume nativo; raras paginas multi-step | **False** por padrão até smoke validado |
| `ats_lever.py` | hosts `jobs.lever.co`, `hire.lever.co`; campos aninhados por `<fieldset>`; "I consent to privacy" | **False** por padrão até smoke validado |
| `ats_gupy.py` | hosts `*.com.vagas.io`, `portal.gupy.io`...; tel BR com máscara; perguntas de diversidade obrigatórias; botão "Finalizar candidatura" | **False** por padrão até smoke validado |

Regra de lançamento: **assisted-first** — todos nascem `auto_submit_capable=False`;
vira `True` por site apenas após smoke assistido aprovado (passo 5 do guia), mesmo
onde o robô tecnicamente conseguiria enviar.

### Template + guia para agentes

- **`ats_template.py`**: classe comentada, 100% copiável, `name="meu_ats"`, hosts
  placeholder, hooks com exemplo inline (dropdown custom, telefone com código de país).
  Importa sem efeitos colaterais: o `@register` do template é **comentado** e só
  descomentado ao renomear (senão polui o registry). Tem de rodar contra um
  formulário HTML bobo usando só o kernel (teste de sanidade da interface).
- **`docs/ats-handler-guide.md`**: passo a passo —
  1. reconhecimento com 2–3 vagas reais (`browser_snapshot`/CDP; hosts, layout, submit);
  2. checklist de quirks (dropdown nativo? telefone? captcha em que passo? i18n? EULA?) —
     tabela a preencher no PR;
  3. criar `ats_<site>.py` do template; **seletores só como constantes no topo do
     arquivo, cada uma com comentário `# evidência: <URL real, observação, data>`**;
  4. testes obrigatórios (`can_handle`, hooks com fake-page, registro no router);
  5. smoke assistido com 1 candidatura real no painel antes de mexer em
     `auto_submit_capable`;
  6. definition of done (1 linha de import em `ats_router`, testes verdes, guia
     atualizado com quirks descobertos).

## Estratégia de testes

`python3 -m unittest discover -s tests`, sem browser real (fake-page):

- **`tests/test_ats_base.py`** — `@register` indexa; `find_handler` casa host ±`www.`
  ±porta; colisão de host/nome explode no import; `ats_template` satisfaz a ABC e o
  default `fill` delega ao kernel.
- **`tests/test_ats_kernel.py`** (fake-page com `locator`/`evaluate` stub — padrão já
  usado em `tests/test_button_engine.py`): match label→regra; `safe_fill` recupera de
  elemento detached; banner de cookie fechado e **submit nunca clicado** na varredura
  de intrusos; passo não avança sem obrigatório; valor sumido por re-render vira
  `missing`.
- **`tests/test_ats_router.py`** — dispatch por URL; `NO_HANDLER` retorna host no erro;
  obstacle detectado → assistido mesmo com `auto_submit_capable=True`; página fechada
  → `FAILED`; mapeamento outcome→notes/status (inclui prefixo `NO_HANDLER:`).
- **`tests/test_ats_inhire.py`** — hooks específicos com fake-page: ordem telefone
  (+55 antes do número), dropdown React PCD, privacidade checkbox. Garante não-regressão
  do refactor.
- Greenhouse/Lever/Gupy: `can_handle` + hooks dos quirks listados na tabela acima.

## Pontos de integração (código existente)

- `linkedin_apply.py:402-470` (`_handle_external_ats`) → `run_ats_flow`;
- `linkedin_apply.py:23`, `app.py:25` — imports de `ats_inhire` substituídos por `ats_router`;
- `app.py:628` (`is_assisted_apply_job`) e `app.py:893` (gate do worth easy apply) →
  `find_handler(...) is not None`;
- `app.py:2197` (`worth_html`) → nota vermelha quando `NO_HANDLER:`;
- `ats_inhire.py:is_inhire_url/fill_inhire_form/wait_for_human_inhire` viram classe
  `InHireHandler` (funções viram helpers privados; `wait_for_human_inhire` promove-se a
  `wait_for_human` genérico no router).

## Riscos conhecidos

- Seletores de Greenhouse/Lever/Gupy chutados sem smoke real → mitigado pelo requisito
  de `# evidência:` + assisted-first.
- Re-âncora de página depende de `context.pages` se comportar igual em pydoll e
  playwright → coberto pelo contrato já existente (ambos expõem a API sync do
  playwright); fake-page não testa isso — smoke real é obrigatório por site novo.
- Colisão de host entre handlers futuros → falha dura no import (decisão explícita,
  não "primeiro ganha").
