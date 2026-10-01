# Conversão salarial para moeda da vaga (FX via API gratuita)

**Data:** 2026-10-01  
**Status:** implementado  
**API:** `https://open.er-api.com/v6/latest/USD` (sem chave; inclui COP). Frankfurter/BCE não publica COP — por isso não é a fonte única.

## Comportamento

1. Detectar moeda do campo (BRL, USD, COP, EUR, GBP, MXN, ARS, CLP, CAD, PEN…).
2. Base: `salary_expectation_usd`; se vazio, BRL convertido via taxa BRL→alvo (via USD).
3. Cache em settings `fx_rates_json` + `fx_rates_fetched_at` (TTL 12h) + cache em memória.
4. Falha de rede: só BRL/USD nativos; outras moedas → vazio.
