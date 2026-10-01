# Copiloto: retry em 429 + modal congelado após Enviar

**Data:** 2026-10-01  
**Status:** aprovado  
**Decisões:** retry in-place no mesmo `copilot_takeover` (preserva `history`/`facts`/página); após Enviar o ask fica `awaiting_ai` com spinner no painel; teto ~10 min de retries de rate-limit; 401/403 abortam na hora.

## Fluxo

1. `ask` → `pending` → modal editável (inalterado).
2. Enviar → grava facts → status **`awaiting_ai`**; modal congela + spinner (cliente + `/live`).
3. Copiloto retoma o mesmo loop; `history` inclui `ask -> answered`; facts do DB.
4. Próximo `call_ai_text`:
   - OK → `answered`; modal some.
   - 429/quota transitória → backoff (`backoff_seconds(..., rate_limited=True)`), hint no painel, **não** limpa contexto; repete até teto 600s.
   - 401/403 ou teto esgotado → `failed_ai` + `abort_close`.
5. Cancelar só em `pending`.

## Dados

Status: `pending` | `awaiting_ai` | `answered` | `cancelled` | `expired` | `failed_ai`  
Coluna `hint` em `copilot_asks` para mensagem de retry no `/live`.

## Escopo

- `copilot_asks`, `ats_copilot` (retry + complete/fail ask), painel modal, `/live`.
- Fora: múltiplas asks; retry via job_queue (abort já engolido).
