# Worth date sort/filter — Implementation Plan

> **For agentic workers:** Execute task-by-task.

**Goal:** Ordenação e intervalo por `first_seen_at` em Vale a pena olhar.

**Architecture:** Estender `worth_html` + `/live` + JS da barra de filtros.

**Tech Stack:** Python/SQLite, unittest

## Global Constraints

- Campo: `first_seen_at`
- Default sort: match DESC
- Datas interpretadas em America/Sao_Paulo

---

### Task 1: Helpers + testes SQL/params

- [ ] Funções de sort/range
- [ ] Testes unitários
- [ ] Integrar em `worth_html` / live / JS
