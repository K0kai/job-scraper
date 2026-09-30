# Handlers ATS plugáveis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Arquitetura de handlers ATS plugáveis (`ats_base` + kernel genérico com salvaguardas de popup + router), refactor InHire, novos Greenhouse/Lever/Gupy, template/guia para agentes e fallback `NO_HANDLER` vermelho no painel.

**Architecture:** ver `docs/superpowers/specs/2026-09-29-ats-handlers-design.md`. Handlers herdam de `BaseATSHandler`, registram-se via `@register`; `ats_kernel` faz o preenchimento com re-âncora/anti-popup; `ats_router` decide auto × assistido × NO_HANDLER.

**Tech Stack:** Python 3.14, unittest, pydoll/playwright (camada sync idêntica), SQLite.

## Global Constraints

- Todo teste roda com `python3 -m unittest discover -s tests`; suíte 100% verde ao fim de cada tarefa.
- Nunca auto-clicar Submit no wizard LinkedIn (política anti-ban inalterada).
- `auto_submit_capable=False` por default em handler novo; vira True só após smoke real.
- Seletores nos handlers vivem como constantes no topo com comentário `# evidência:`.
- Commits `feat:`/`fix:`/`refactor:`/`docs:` direto em `master`.

---

### Task 0: Helpers rápidos em `browser_engine.py` (pendência da sessão anterior)

**Files:** Modify `browser_engine.py`; Test `tests/test_button_engine.py` (já existe, RED).
**Produces:** `_is_root`, `chrome_launch_args(root_no_sandbox=True, extra=None)`, `persistent_launch_kwargs(profile, headless, slow_mo, viewport, locale, args, **extra)`, `scan_buttons(page)`, `find_first_button(buttons, patterns, *, exclude=None, tag=None)`, `wait_for_matching_button(page, patterns, *, timeout_ms, interval_s, exclude=None)`, `click_scanned_button(page, index)`, `_poll_sleep`.

- [x] Rodar `python3 -m unittest tests.test_button_engine -v` (falha RED)
- [x] Implementar os helpers acima
- [x] Suíte verde; `linkedin_apply.py:795-808` passa a usar `persistent_launch_kwargs` (ganha `--no-sandbox` root); `apply_channels.py:292` launch headless ganha `chrome_launch_args()`
- [x] Commit `feat: helpers de launch/scan de botões com --no-sandbox root`

### Task 1: `ats_base.py` — contrato + registro

**Files:** Create `ats_base.py`; Test `tests/test_ats_base.py`.
**Produces:** `ApplyContext(cfg, rules, resume_path, cover_letter, salary="")`, `FillResult(ok, filled, missing, error)`, `SubmitResult(clicked, button_label, error)`, `BaseATSHandler` (attrs `name/hosts/auto_submit_capable=False/success_regex`; métodos `can_handle(url)`, `wait_ready(page, timeout_ms=15000)`, `detect_obstacles(page)`, `fill(page, ctx)`, `submit(page)`, `verify_submitted(page)`, hooks `pre_fill`/`post_fill`/`advance_step`), `register(cls)`, `find_handler(url)`, `HANDLERS`.
- `can_handle`: casa netloc ±`www.` ±porta contra `hosts`; entrada `"*.dominio.com"` = sufixo.
- Colisão de `name`/host entre handlers → `RuntimeError` no import.

- [x] Testes: registro/busca host±www±porta, wildcard, colisão explode, defaults da ABC
- [x] Implementar → verde → commit `feat: ats_base com registro de handlers`

### Task 2: `ats_kernel.py` — preenchimento + salvaguardas

**Files:** Create `ats_kernel.py`; Test `tests/test_ats_kernel.py` (fake-page `mock.MagicMock`, padrão `test_button_engine`).
**Produces:** `collect_fields(page)`, `dismiss_intruders(page)`, `safe_fill(page, selector, value)`, `fill_page(page, ctx, handler=None) -> FillResult`, `find_submit_button(page)`, `click_submit(page) -> SubmitResult`.
- `INTRUDER_JS` fecha cookie-banner/overlay por texto de fechar, **lista negra** `SUBMIT_LABEL_RE` nunca clicada.
- `safe_fill`: scroll→click→fill; falha → `dismiss_intruders` + relocalizar + retry 1; 2ª falha → `missing`.
- `fill_page`: loop de passos ≤8, só avança "Next/Continuar" sem obrigatórios em `missing`; `missing` não-vazio ⇒ não avança; respeita `handler.advance_step`.
- Kernel nunca lança — exceção vira `FillResult(error=...)`.

- [x] Testes por comportamento (match regra, detached recupera, submit nunca clicado, passo travado, page fechada → error) → implementar → verde → commit `feat: kernel de preenchimento com salvaguardas anti-popup`

### Task 3: `ats_router.py` — outcomes e fluxo externo

**Files:** Create `ats_router.py`, `wait_human.py` (funções `wait_for_human(page, minutes, success_regex=SUCCESS_RE)`, `DEFAULT_SUCCESS_RE`).
**Produces:** `OUTCOME_SUBMITTED/ASSISTED/TIMEOUT/FAILED/NO_HANDLER`, `run_ats_flow(page, context, cfg, rules, resume_path, cover_letter, salary, human_wait) -> tuple[outcome, detail]`, re-export `find_handler`.
- Re-âncora via `context.pages` (≤10s) → sem handler: `NO_HANDLER: apply externo em <host> — nenhum driver ATS instalado.`
- Handler: `wait_ready`→`fill`→`detect_obstacles`: sem obstáculo e `auto_submit_capable` → `submit`→`verify_submitted` (≤20s) → SUBMITTED; senão assistido (wait_for_human) → ASSISTED/TIMEOUT; página fechada em qualquer ponto → FAILED.
- Importa/atualiza lista de handlers (`ats_inhire`, `ats_greenhouse`, `ats_lever`, `ats_gupy`) — um por um conforme existirem.

- [x] Testes de dispatch/casos acima com handler fake → implementar → verde → commit `feat: ats_router com outcomes e wait_for_human genérico`

### Task 4: Template + guia

**Files:** Create `ats_template.py`, `docs/ats-handler-guide.md`.
- Template: classe comentada `@register` (comentado para não poluir registry), exemplo de `post_fill` (dropdown custom) e constantes `# evidência:`; passa como sanidade no teste de interface.
- Guia: 6 passos da spec (reconhecimento, checklist de quirks, seleção com evidência, testes, smoke assistido, DoD).

- [x] Commit `docs: template e guia de handlers ATS`

### Task 5: Refactor InHire para `InHireHandler`

**Files:** Rewrite `ats_inhire.py` (classe; helpers privados movidos); Test `tests/test_ats_inhire.py`.
- `auto_submit_capable=False`; `post_fill` faz dropdowns React/telefone/PCD; capta `SUCCESS_RE` no handler.
- [x] Testes: `can_handle('https://xxx.inhire.app/...')`, ordem +55 antes do número (`_local_phone_digits`), LinkedIn URL normalization → verde → commit `refactor: InHire como handler da nova interface`

### Task 6: Handlers Greenhouse, Lever, Gupy

**Files:** Create `ats_greenhouse.py`, `ats_lever.py`, `ats_gupy.py`; Test `tests/test_ats_new_handlers.py`.
- Greenhouse: hosts `boards.greenhouse.io`, `job-boards.greenhouse.io`; consent/EULA checkbox em `post_fill`.
- Lever: `jobs.lever.co`, `hire.lever.co`; privacy consent; submit label "Submit application".
- Gupy: `*.gupy.io`, `portal.gruponossa.com.br`... (hosts conforme guia; wildcard suportado); tel BR máscara; "Finalizar candidatura".
- [x] `can_handle` por host + `auto_submit_capable=False` asserted → verde → commit `feat: handlers greenhouse, lever, gupy (assisted-first)`

### Task 7: Integração `linkedin_apply.py` + `app.py` + painel

**Files:** Modify `linkedin_apply.py` (`_handle_external_ats` → `run_ats_flow`; branch InHire direto → `find_handler`; linha 919/949 idem), `app.py` (`is_assisted_apply_job:628`, gate `893`, `worth_html:2197` nota `NO_HANDLER:` vermelha via `<p class="hint" style="color:#b3261e;font-weight:600">`).
**Produces:** outcome→UI: SUBMITTED→finish_ok "enviada automaticamente"; ASSISTED/TIMEOUT→finish_ok como hoje; FAILED→block(detail); NO_HANDLER→block com detalhe `NO_HANDLER:` (flui para notes 'worth').
- [x] Ajustar testes existentes que citam InHire; suíte completa verde → commit `feat: roteamento ATS plugável no linkedin_apply e painel`

### Task 8: Verificação final

- [x] `python3 -m unittest discover -s tests` 100% verde
- [x] Smoke de import: `python3 -c "import app, ats_router"` sem efeitos colaterais
- [x] Commit final de fixos necessários
