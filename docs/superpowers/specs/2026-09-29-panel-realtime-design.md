# Design: Painel com atualização em tempo real (processos)

**Data:** 2026-09-29  
**Status:** aprovado (abordagem A — ampliar `/live` + POST de processo via `fetch`)

## Objetivo

Tornar o painel responsivo sem F5: status de processos (coleta, currículos, filas, vagas, logs) atualiza sozinho, e ações de processo não recarregam a página inteira.

## Fora de escopo

- SSE / WebSocket / SPA
- Salvar settings/IA/SMTP/regras sem reload (POST clássico permanece)
- Atualizar ao vivo campos de formulário de configuração
- Libs front-end novas

## Estado atual

- `GET /live` (JSON) + polling JS a cada 1,5s já atualiza: coletor, stats, tabela de vagas, histórico, filas, “Vale a pena olhar”, logs
- **Não** atualiza o bloco de currículos (badge/dossiê)
- POSTs de processo fazem redirect HTML → reload completo

## Abordagem

Ampliar o live existente e tornar só rotas de **processo** AJAX-aware.

### 1. Live — zona de status do currículo

Cada card de currículo tem duas zonas:

| Zona | Conteúdo | Atualização |
|------|----------|-------------|
| Upload | form PDF + checkbox forçar | Estático no HTML inicial; após upload AJAX ok, limpar `input[type=file]` |
| Status | `#resume-status-pt` / `#resume-status-en` — badge, mensagem, reanalisar, dossiê/erro | Via `/live` |

`live_payload()` passa a incluir:

```json
{
  "resume_status_html": { "pt": "...", "en": "..." }
}
```

O JS aplica só se `innerHTML` diferir. Não inclui o `<input type="file">` no HTML live (evita perder arquivo escolhido).

Polling: mantém ~1500 ms; refresh imediato em `visibilitychange` (já existe). Aba ativa via `localStorage` não muda no refresh.

### 2. Ações de processo via `fetch`

**AJAX (sem reload):**

- `/upload-resume`
- `/reanalyze-resume`
- `/start`
- `/stop`
- `/queue-cancel`
- `/queue-retry`
- `/queue-clear`
- `/clear-logs`

**Não entram no AJAX nesta entrega:** `/job-status`, `/notes` (continuam POST clássico).

**POST clássico (fora):** `/settings`, `/ai-settings`, `/smtp-settings`, `/smtp-test`, `/profile-settings`.

**Contrato:** se o request indicar JSON (`Accept: application/json` ou header `X-Requested-With: fetch`), responder:

```json
{ "ok": true, "notice": "mensagem", "kind": "success|info|error|warning" }
```

Caso contrário, manter redirect HTML atual (compatibilidade).

**Cliente:**

- Interceptar `submit` dos forms das rotas acima com `fetch` + `FormData`
- Atualizar banner `#panel-notice` com a classe de kind
- Chamar `refresh()` imediatamente
- Upload ok → limpar file input daquele form
- Desabilitar botão durante o request (anti double-submit)
- Falha de rede → notice `error` sem reload

### 3. UX

- Notice some após ~6s ou na próxima ação
- Logs: sticky-bottom atual; não forçar scroll se o usuário leu o meio
- Filas/jobs/vale: substituir HTML só quando mudar
- Cancel/retry via AJAX; o live redesenha a tabela da fila

## Arquivos tocados

- Principalmente `app.py` (HTML dos cards, `live_payload`, `send_json`/helpers de notice, handlers POST, JS inline)
- Sem mudança de schema SQLite nem de `resume_pipeline` (salvo se precisar extrair helper de HTML de status)

## Critérios de aceite

1. Com análise `pending`, o badge/dossiê do currículo muda para OK/erro **sem F5**
2. Upload / reanalisar / start / stop / cancelar-reenfileirar fila / limpar logs **não** recarregam a página; notice aparece no topo
3. Escolher PDF e esperar o poll **não** limpa o arquivo selecionado
4. Settings/IA/SMTP/regras continuam com reload clássico
5. Aba Filas/Logs/Vale continua atualizando como hoje, agora também com currículo
