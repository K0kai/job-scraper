# Progresso detalhado do Easy Apply na fila

**Data:** 2026-10-01  
**Status:** implementar  
**Decisão:** histórico append-only em `progress_log`; `result`/`last_error` não apagam.

## Escopo

- Coluna `progress_log` + `JobQueue.append_progress`
- UI da fila: etapa atual + histórico
- Hooks em linkedin_apply, ats_router, salary_review, ats_copilot
