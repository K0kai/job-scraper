# Salário: período + review do copiloto (política offshore)

**Data:** 2026-10-01  
**Status:** aprovado  
**Decisões:** bot preenche (FX + período); copiloto **sempre revisa** salário; mercado = julgamento da IA com guardrails do painel (sem API de bands); 429 no review não aborta a candidatura.

## Fluxo

1. Detectar moeda (existente) + **período** do label (`year`/`month`/`hour`/`unknown`).
2. Proposta do bot: base `salary_expectation_usd` (senão BRL) → FX → normalizar ao período do campo.
3. `salary_review` (IA curta) recebe proposta + vaga + perfil + política:
   - piso ≈ pretensão BR convertida
   - alvo ≈ um pouco abaixo do mercado local do país (mão de obra remota / ~10–20%)
   - preferir âncora USD remota do painel quando existir
4. Resposta: `ok` | `adjust` | `ask` (modal copiloto).
5. Review falha/429 → mantém proposta do bot + log.
6. Travamento geral do form → `copilot_rescue` (inalterado); prompt do takeover inclui a mesma política.

## Módulos

- `salary_policy.py` — período, normalização, proposta (puro).
- Review — prompt JSON + parse; hook no fill de modo `salary` (kernel/LinkedIn/ATS).
- Atualizar prompt do `ats_copilot` com a política.

## Fora de escopo

- API/tabelas de salary bands por país.
- Review de campos que não são salário.
