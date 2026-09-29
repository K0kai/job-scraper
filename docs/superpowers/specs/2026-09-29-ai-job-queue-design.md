# Design: Fila assíncrona com retry para operações de IA

**Data:** 2026-09-29  
**Decisão:** 3 workers paralelos (aprovado)

## Objetivo

Evitar falha definitiva quando a API de IA (plano grátis) estiver em fila, rate-limit ou indisponível. Análise de currículo e candidatura automática entram numa fila persistente com retry até sucesso, expiração ou cancelamento manual.

## Escopo

- Jobs: `resume_analysis`, `job_apply`
- Tabela SQLite `queue_jobs`
- Classe `JobQueue` + worker `ThreadPoolExecutor(max_workers=3)`
- Aba **Filas** no painel: listar, cancelar, reenfileirar, limpar concluídos
- Settings: `queue_max_workers=3`, `queue_max_attempts=40`, `queue_ttl_hours=24`

## Retry

- Em `AiUnavailableError` / HTTP 429 / 503 / mensagens de quota: status `retry_wait`, backoff 30s→60s→120s→300s→… (teto 900s)
- Outros erros de validação (PDF inválido, sem currículo): `failed` sem retry
- `expires_at` = created + TTL; após isso `failed`

## Fluxos

1. Upload PDF → salva arquivo + texto → enfileira `resume_analysis` (HTTP retorna rápido)
2. Auto-apply → enfileira `job_apply` por vaga `new` (não processa inline no coletor)
3. Worker processa em paralelo (até 3), loga na aba Logs

## Fora de escopo

- Filas distribuídas / Redis
- Mais de 3 workers por padrão (configurável no settings)
