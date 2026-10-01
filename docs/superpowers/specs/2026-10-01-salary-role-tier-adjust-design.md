# Salário: abaixar pretensão irrealista (estágio/júnior)

**Data:** 2026-10-01  
**Status:** aprovado — implementar  
**Decisão:** IA pode descer abaixo do piso do painel em intern/junior (BR e exterior).

## Mudanças

1. `detect_role_tier(job_text)` → `intern` | `junior` | `standard`
2. `needs_salary_ai_review` True também para intern/junior
3. Política de review: piso do painel só para standard; intern/junior ajustam para baixo se irrealista
4. Heurística bot: cap mensal BRL/USD para intern antes da IA
5. Prompt do copiloto alinhado
