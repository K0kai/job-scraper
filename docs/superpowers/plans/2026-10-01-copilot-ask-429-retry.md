# Copilot ask 429 retry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Em 429, o copiloto retenta no mesmo loop (contexto intacto); após Enviar o modal fica congelado com spinner até a IA aceitar o próximo turno ou esgotar o teto.

**Architecture:** `answer_ask` → `awaiting_ai`; helper de retry com backoff em `ats_copilot`; `/live` e JS do modal refletem `awaiting_ai` + `hint`.

**Tech Stack:** Python/SQLite, painel HTML/JS em `app.py`, unittest.

## Global Constraints

- Teto de retry de rate-limit: 600s
- Só retenta rate-limit (429/quota); auth aborta
- Preservar `history`/`facts` entre retries

---

### Task 1: Status `awaiting_ai` + hint em `copilot_asks`

**Files:**
- Modify: `copilot_asks.py`
- Modify: `tests/test_copilot_asks.py`

- [ ] Test: answer → awaiting_ai; panel ask ainda visível; complete → some; wait aceita awaiting_ai
- [ ] Implementar statuses, hint, complete/fail/set_hint
- [ ] Atualizar testes existentes

### Task 2: Retry de IA no copiloto

**Files:**
- Modify: `ats_copilot.py`
- Modify: `tests/test_ats_copilot.py` (ou novo teste)

- [ ] Test: 429 depois 429 depois OK → sucesso; history tem entradas de retry; ask completed
- [ ] Test: 401 → abort sem retry infinito
- [ ] Implementar `call_ai_with_rate_limit_retry` + wire no takeover

### Task 3: Painel modal spinner

**Files:**
- Modify: `app.py` (HTML/CSS/JS + `/live` já via get_pending_ask)

- [ ] Modal: spinner overlay; freeze em awaiting_ai; freeze imediato no Enviar
