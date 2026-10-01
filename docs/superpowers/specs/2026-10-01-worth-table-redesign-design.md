# Design: Vale a pena olhar — tabela + toolbar única

**Data:** 2026-10-01  
**Decisão:** toolbar compacta + tabela; sort por header com ciclo 3 estados **por coluna**, combináveis

## Objetivo

Uma fileira de filtros; lista em tabela. Match / Encontrada / Publicada ordenam por clique (nenhum → ↑ → ↓ → nenhum em cada coluna). Várias colunas ativas ao mesmo tempo; `ORDER BY` combina todas. Detalhes em `<details>`.

## Sort multi-coluna

- Encoding: `match_desc,seen_asc` (vírgula; primeiro = prioridade)
- Ciclar uma coluna não remove as demais
- Última coluna clicada vai para a frente (prioridade principal)
- Sem sort: `jobs.id DESC`
- Filtros de intervalo/match mínimo continuam independentes e simultâneos com a ordenação
