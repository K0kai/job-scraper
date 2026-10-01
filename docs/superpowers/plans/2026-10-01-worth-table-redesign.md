# Worth table redesign — Implementation Plan

> **For agentic workers:** Execute task-by-task.

**Goal:** Toolbar única + tabela com sort por header (ciclo 3 estados).

**Architecture:** Refatorar `worth_html`; sort keys `match_asc|match_desc|seen_asc|seen_desc|posted_asc|posted_desc|""`; JS clique em `th[data-worth-sort-col]`.

**Tech Stack:** Python/SQLite, unittest

## Global Constraints

- Uma fileira de filtros
- Clique: none → asc → desc → none
- Detalhes via `<details>`

---

### Task 1: Helpers + testes + worth_html tabela + JS
