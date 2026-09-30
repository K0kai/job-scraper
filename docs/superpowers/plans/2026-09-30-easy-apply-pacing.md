# Easy Apply pacing + single concurrency Implementation Plan

> **For agentic workers:** Execute task-by-task. Steps use checkbox syntax.

**Goal:** Remover limite diário do Easy Apply; gap 1–30 (default 3); no máximo um `linkedin_apply` running na fila.

**Architecture:** Pacing só em `rate_limit_block_reason`. Exclusão de kind no `_claim_batch` da `JobQueue`.

**Tech Stack:** Python, SQLite queue, unittest

## Global Constraints

- Gap: piso 1, default 3, teto 30
- Sem limite diário
- Máx. 1 `KIND_LINKEDIN` em `running`

---

### Task 1: Testes de pacing

**Files:**
- Create: `tests/test_linkedin_pacing.py`
- Modify: `linkedin_apply.py`

- [ ] Teste: gap bloqueia; sem daily even com muitas ações no dia
- [ ] Clamp 1–30 / default 3
- [ ] Implementar pacing sem daily
- [ ] Rodar testes

### Task 2: Exclusão na fila

**Files:**
- Modify: `job_queue.py`, `tests/test_job_queue.py`
- Modify: `app.py` defaults

- [ ] Teste: dois linkedin pending → claim só um se já há running / no mesmo batch
- [ ] Implementar filtro no `_claim_batch`
- [ ] Atualizar defaults app.py
- [ ] Rodar suite relevante
