# Design: Vale a pena olhar — ordenação e intervalo

**Data:** 2026-10-01  
**Decisão:** abordagem 1 — controles na barra + query params no `/live`

## Objetivo

Ordenar por match ou por data encontrada (`first_seen_at`), cada um ↑/↓, e filtrar por intervalo de datas + match mínimo — todos combináveis ao mesmo tempo.

## Escopo

- Ordenação: `match` (DESC, default), `match_asc`, `seen_desc`, `seen_asc`
- Intervalo date from/to em `first_seen_at` (dias BRT → UTC)
- Match mínimo existente
- Params `/live` + `localStorage`
- Página volta a 1 ao mudar filtros
- Testes unitários

## Fora de escopo

- Ordenar/filtrar por `posted_at`
- Preferências no SQLite
