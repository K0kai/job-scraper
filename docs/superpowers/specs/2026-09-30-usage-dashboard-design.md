# Design: Aba Uso & cotas (Apify + IA local)

**Data:** 2026-09-30  
**Decisão:** abordagem 1 — nova aba na sidebar; Apify via API; IA = estimativa local do provedor ativo

## Objetivo

Mostrar uso/limite do plano Apify e consumo estimado da IA selecionada no painel (Gemini ou OpenAI), com aviso explícito de que a parte de IA é estimativa local deste app.

## Escopo

- Nova aba sidebar `uso` / **Uso & cotas**
- **Apify:** `GET /v2/users/me/limits` (+ usage se útil); comparar com `apify_monthly_credit_limit_usd`; cache em settings; botão Atualizar
- **IA:** contadores locais por dia UTC e mês UTC: chamadas ok, falhas quota, tokens in/out (quando a API devolver); provedor/modelo atuais do settings
- Instrumentar `ai_client.call_ai_text` e caminhos Gemini/OpenAI em `resume_pipeline` / `app.py` que não passem por ele
- Copy: “Estimativa local deste app — não é a cota oficial do provedor”
- Não consultar Apify no polling `/live` (só cache + refresh explícito ou ao abrir/atualizar a aba)

## Fora de escopo

- Cota restante oficial Gemini/OpenAI via API key
- Gráficos históricos detalhados
- Contadores por OpenAI se provedor for Gemini (mostrar só o provedor ativo; histórico pode ser por chave provider)
