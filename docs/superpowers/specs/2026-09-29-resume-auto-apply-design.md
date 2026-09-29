# Design: Currículo, match e candidatura automática

**Data:** 2026-09-29  
**Status:** aprovado (brainstorming + pedido de implementação)

## Objetivo

Permitir upload de currículo PDF (PT e EN) via seletor de arquivos do SO, analisar cada PDF **uma vez por conteúdo (hash)**, guardar a análise, comparar com cada vaga e, se o match for bom e `auto_apply` estiver ativo, candidatar enviando PDF + carta — por **SMTP** (se houver e-mail na vaga) ou **Playwright** em formulário público (sem login).

## Fora de escopo

- Login em LinkedIn/ATS, CAPTCHA, bypass de autenticação
- Inventar experiência além da análise do currículo
- Reanalisar o PDF em cada vaga

## Modelo de dados

### Tabela `resumes` (1 linha por idioma `pt`/`en`)

- `language` UNIQUE, `original_filename`, `stored_path`, `file_sha256`
- `extracted_text`, `analysis_json`, `analysis_summary`
- `analyzed_at`, `provider`, `model`, `created_at`, `updated_at`

### Tabela `form_field_rules`

- `key`, `aliases` (CSV), `mode` (`text`|`select`|`file`|`cover_letter`|`skip`), `value`, `sort_order`

### Tabela `applications`

- `job_id`, `channel` (`email`|`browser`), `recipient`, `resume_id`, `cover_letter_id`
- `status` (`sent`|`failed`|`blocked`), `detail`, `attempted_at`

### Tabela `form_answers`

- `job_id`, `question`, `answer`, `provider`, `model`, `created_at`

### `ai_decisions` (colunas novas)

- `resume_language`, `apply_channel`, `apply_result`

### Settings / keyring

- Settings: `smtp_host`, `smtp_port`, `smtp_user`, `smtp_from`, `smtp_use_tls`, perfil (`candidate_phone`, `candidate_linkedin`, `candidate_city`, …)
- Keyring: `smtp_password` (+ chaves já existentes)

## Fluxos

### Upload

1. `POST /upload-resume` multipart (`language` + PDF)
2. Hash SHA-256; se igual ao registro do idioma → não chama IA
3. Senão: salva em `resumes/`, extrai texto (`pypdf`), analisa com IA uma vez, persiste

### Match por vaga

1. Coletor, após busca, se `auto_apply=1`, processa vagas `new` até `maximum_applications_per_run`
2. `ai_assess_job` usa `analysis_summary` (+ fatos) × descrição; escolhe idioma do CV
3. Score &lt; mínimo ou `should_apply=false` → `ignored`
4. Qualifica → gera carta → tenta aplicar

### Aplicação

1. **E-mail:** extrai e-mails da descrição; SMTP com carta no corpo e PDF anexo
2. **Browser:** Playwright sem login; regras de formulário; selects por match de texto
3. **Pergunta aberta:** IA gera resposta ancorada na análise; se IA indisponível (sem chave/quota/erro de auth) → **pula vaga** (`blocked`), não envia formulário parcial
4. Sem canal → `blocked`

## UI / rotas

- Seções: Currículos, Perfil/regras de formulário, SMTP (+ teste)
- Rotas: `/upload-resume`, `/profile-settings`, `/smtp-settings`, `/smtp-test` + existentes

## Dependências

- `pypdf`, `playwright` (+ browsers instalados pelo usuário)
- `keyring`, `lingua-language-detector` (já listados)
