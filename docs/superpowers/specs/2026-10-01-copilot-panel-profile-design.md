# Copiloto: perfil do painel + extrato priorizado do currículo

**Data:** 2026-10-01  
**Status:** implementado  
**Contexto:** `COPILOT_UNAUTOMATED` em vaga real — IA abortou com *"Cannot answer location, legal authorization, university, GPA, and salary…"* apesar de cidade/salário existirem no painel e formação/localidade existirem na análise do currículo.

## Problema

1. O prompt do copiloto (`ats_copilot.build_copilot_prompt`) recebe só `candidate_facts_*` + um clip cego de `resume_json`/`resume_summary` (1600 chars). Campos estruturados do painel (`candidate_city`, `salary_expectation_*`, telefone, etc.) **não entram**.
2. O clip cego corta o final do JSON: `education`, `location_notes` e `work_authorization_notes` ficam no fim da análise e somem com frequência. A IA não “falha em inferir” — muitas vezes **não recebe** o dado.
3. Política de abort: quando ainda faltar dado obrigatório (ex.: GPA sem menção), **abortar e fechar** permanece (escolha explícita do usuário). Sem mudança de UX nesse ponto.

## Objetivo

O copiloto deve preencher location / university / salary / work-auth quando o dado existir no **painel** ou no **extrato priorizado do currículo**. Só emitir `abort` se o campo for obrigatório e **não** houver fonte em nenhum dos dois.

## Fora de escopo

- Não mudar a política abort+fechar → nota amarela.
- Não inventar GPA, autorização legal ou consentimento se não houver evidência.
- Não aumentar o clip bruto genérico sem priorização (abordagem C rejeitada).
- Não alterar handlers ATS / form_rules além do necessário para passar `cfg` (já disponível em `copilot_takeover`).

## Design

### 1. `panel_profile_block(cfg: dict) -> str` (pura)

Monta um bloco texto curto a partir do painel, omitindo vazios:

| Campo painel | Label no bloco |
|---|---|
| `candidate_name` | name |
| `candidate_email` | email |
| `candidate_phone` | phone |
| `candidate_linkedin` | linkedin |
| `candidate_city` | city |
| `candidate_cpf` | cpf |
| `salary_expectation_brl` | salary_brl |
| `salary_expectation_usd` | salary_usd |
| `candidate_contract_type` | contract_type |

Não re-incluir `candidate_facts_*` aqui (já vão em `=== CANDIDATE FACTS ===`). Diversidade continua só via facts/perfil existente — não expandir neste PR.

### 2. `copilot_resume_excerpt(resume_json: str, resume_summary: str, *, budget: int = 2000) -> str` (pura)

Se `resume_json` parsear como dict, montar extrato nesta **ordem de prioridade** (cada seção limitada):

1. `location_notes`
2. `work_authorization_notes`
3. `education` (lista → linhas)
4. `headline` / `seniority` / `years_of_experience`
5. skills/tools (cap ~12 itens)
6. experiência recente (até ~3 itens, cada um truncado)

Cortar o resultado final em `budget` chars.  
Se JSON inválido/vazio: fallback `_clip(resume_summary or resume_json, budget)`.

### 3. Mudanças em `build_copilot_prompt`

- Novo parâmetro `panel_profile: str = ""`.
- Inserir seção `=== PANEL PROFILE ===` logo após `CANDIDATE FACTS` (ou antes — ordem sugerida: FACTS → PANEL → RESUME EXCERPT → JOB → PAGE).
- Substituir `dossier = _clip(resume_json or resume_summary, 1600)` por `copilot_resume_excerpt(...)`.
- Título da seção de currículo: `=== RESUME EXCERPT (prioritized) ===`.

### 4. Regras no `ACTION_VOCAB` (ajuste mínimo)

Acrescentar bullet:

- Prefer PANEL PROFILE for city/salary/contact; prefer RESUME EXCERPT for education / location_notes / work_authorization. Only `abort` when a required field has **no** value in either source (e.g. GPA never mentioned). Never invent.

### 5. `copilot_takeover`

- Chamar `panel_profile_block(cfg)` e passar para `build_copilot_prompt`.
- Passar `resume_json`/`resume_summary` para `copilot_resume_excerpt` (já disponíveis em `ai`).

### 6. Abort+fechar

Inalterado: `action == "abort"` → `abort_close` → `COPILOT_UNAUTOMATED: …`.

## Testes

Arquivo sugerido: `tests/test_ats_copilot.py` (ou estender se já existir cobertura parcial).

- `panel_profile_block`: omite vazios; inclui city + salary quando setados.
- `copilot_resume_excerpt`: JSON longo com skills no início e `education`/`location_notes` no fim → extrato **contém** education e location mesmo com budget apertado; clip cego de 1600 do JSON bruto **não** conteria (assert regressão).
- `build_copilot_prompt`: contém `PANEL PROFILE` e `RESUME EXCERPT`; salary/city do painel aparecem no texto.

## Critérios de sucesso

- Com cidade/salário no painel, o prompt do copiloto os lista explicitamente.
- Com `education` + `location_notes` só no final do `analysis_json`, o extrato ainda os inclui sob budget 2000.
- Abort+fechar e prefixo `COPILOT_UNAUTOMATED:` permanecem iguais.
- Sem mudança de comportamento quando painel e extrato estão vazios (IA ainda pode abortar — esperado).

## Riscos

- Extrato priorizado pode omitir detalhes de experiência que a IA usaria para outras perguntas → mitigado mantendo ~3 experiences + skills.
- Tokens sobem um pouco (painel ~200 chars + budget 2000 vs 1600) — aceitável.
