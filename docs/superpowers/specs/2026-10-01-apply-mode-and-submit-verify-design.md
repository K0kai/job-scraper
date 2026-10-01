# Modo de candidatura review × auto + anti falso-positivo

**Data:** 2026-10-01  
**Status:** implementar (pedido explícito do usuário)

## Modos (`apply_mode`)

| Valor | Comportamento |
|---|---|
| `review` (default) | Preenche; **nunca** clica Enviar/Submit; espera humana; sucesso só com regex estrita |
| `auto` | Preenche e envia sozinho quando possível; copiloto `allow_submit`; handlers tentam submit se sem obstáculo |

## Anti falso-positivo

- Remover de InHire: `sucesso` solto, `continue registration`
- Apertar DEFAULT / LinkedIn SUCCESS_RE
- Copiloto: após clique Submit, só `SUBMITTED` se `verify`/regex de sucesso confirmar (≤20s); senão `SOLVED`/`timeout` assistido

## UI

Radio na aba Automação: Revisar e enviar eu mesmo / Enviar automaticamente.
