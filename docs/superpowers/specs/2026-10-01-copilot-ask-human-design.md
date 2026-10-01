# Copiloto: pedir dado faltante via modal no painel

**Data:** 2026-10-01  
**Status:** implementado  
**Decisões:** Chrome aberto e pausado; resposta grava em `candidate_facts_*`; timeout → abort+fechar; som de notificação configurável no painel.

## Fluxo

1. Copiloto emite `{"action":"ask","args":{"question":"..."}}`.
2. Grava linha `pending` em `copilot_asks`.
3. Loop espera resposta (poll DB) até `linkedin_human_wait_minutes` (ou default 12).
4. `/live` inclui ask pendente → modal no painel; se `copilot_ask_sound=1`, toca bip (Web Audio) na 1ª detecção do id.
5. POST resposta → append facts PT+EN + `answered`; Cancelar → `cancelled` → abort.
6. Próximo turno do copiloto recarrega facts do DB/cfg.

## Config (aba Automação)

- `copilot_ask_sound` (default `1`) — liga/desliga o som
- `copilot_ask_sound_volume` (0.0–1.0, UI 0–100) — volume

## Escopo

- Tabela `copilot_asks`, endpoints, modal, ação `ask`, som.
- Fora: múltiplas asks paralelas; gravar em campos estruturados além de facts.
