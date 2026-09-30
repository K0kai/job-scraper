# Uso & cotas — Implementation Plan

> **For agentic workers:** Execute task-by-task.

**Goal:** Aba Uso & cotas com Apify (API) e estimativa local da IA ativa.

**Architecture:** Módulo `usage_tracker.py`; instrumentar `ai_client` + callers HTTP duplicados; UI em `app.py`.

**Tech Stack:** Python, SQLite settings, unittest

## Global Constraints

- Estimativa local de IA — copy explícito
- Apify não no polling /live contínuo (cache + refresh)
- Provedor = settings `ai_provider` atual

---

### Task 1: usage_tracker + testes

- [ ] Criar `usage_tracker.py` (record AI, snapshot Apify)
- [ ] Testes unitários
- [ ] Implementar até verde

### Task 2: Instrumentar IA

- [ ] `ai_client.call_ai_text` registra tokens/erros
- [ ] `resume_pipeline` + hot paths em `app.py` registram

### Task 3: Painel

- [ ] Aba + HTML + `/usage-refresh` + live hash
- [ ] Testes de HTML/helpers
