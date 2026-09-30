# Design: Easy Apply — pacing só por intervalo + 1 por vez na fila

**Data:** 2026-09-30  
**Decisão:** abordagem 1 (aprovada) — gap configurável 1–30 min (default 3); sem limite diário; no máximo um `linkedin_apply` em execução na fila

## Objetivo

Remover o teto diário de Easy Apply LinkedIn. Manter apenas intervalo mínimo entre ações, com piso/teto menores. Garantir que a fila processe no máximo um Easy Apply por vez (outros kinds continuam em paralelo).

## Escopo

- `linkedin_apply.rate_limit_block_reason`: só gap (`linkedin_min_gap_minutes`)
- Constantes: piso 1, default 3, teto 30 minutos
- Remover uso de `linkedin_max_per_day` / `HARD_MAX_PER_DAY` / `DEFAULT_MAX_PER_DAY`
- Defaults em `app.py`: sem `linkedin_max_per_day`; `linkedin_min_gap_minutes=3`
- `JobQueue._claim_batch`: não claimar segundo `KIND_LINKEDIN` se já houver um `running` (DB ou neste batch)
- Testes unitários de pacing e de exclusão na fila

## Comportamento

1. **Gap:** se a última ação `channel='linkedin'` for mais recente que o gap, bloquear com mensagem de intervalo; o handler levanta `RuntimeError` (“pacing”) para a fila entrar em `retry_wait`.
2. **Sem limite diário:** quantidade de Easy Applies no dia não bloqueia.
3. **Concorrência:** com N workers, no máximo 1 job `linkedin_apply` em `running` ao mesmo tempo; jobs LinkedIn extras ficam `pending` até o atual terminar.
4. **Lock de Chrome** (`_LINKEDIN_LOCK`) permanece como defesa adicional no handler.

## Correção colateral

O commit que removeu `inhire_pcd` apagou por engano o `<div id="tab-automacao">` inteiro. Restaurar a aba sem o checkbox PCD (PCD fica só em Perfil via `candidate_pcd`). Remover campo “Máx. Easy Apply / dia” da UI.

## Fora de escopo

- Mudança de `queue_max_workers`
- Remoção do lock de perfil Chrome
