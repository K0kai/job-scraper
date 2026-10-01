# Design: Vale a pena olhar — tabela + toolbar única

**Data:** 2026-10-01  
**Decisão:** abordagem 1 — toolbar compacta + tabela; sort por clique no header (ciclo 3 estados)

## Objetivo

Modernizar a aba: uma única fileira de filtros (sem select de ordenação), lista em tabela, ordenação Match / Encontrada / Publicada por clique no cabeçalho (nenhum → ↑ → ↓ → nenhum). Detalhes (nota, descrição, carta) em `<details>` na linha.

## Escopo

- Toolbar: match mín., de/até (`first_seen_at`), presets, Ignorar todas, pager
- Tabela: Match | Vaga | Fonte | Encontrada | Publicada | Detalhes | Ações
- Sort cycle por coluna: `""` | `*_asc` | `*_desc` (só uma coluna ativa)
- Default sem sort: `jobs.id DESC`
- Publicada: `ORDER BY posted_at` com nulos por último
- Remover select de ordenação e cards; remover pager duplicado no rodapé (só na toolbar)
- `/live` + localStorage atualizados
- Testes de cycle/SQL e HTML da tabela

## Fora de escopo

- Drawer/painel lateral
- Sort multi-coluna
