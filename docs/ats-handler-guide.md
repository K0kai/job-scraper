# Guia: como adicionar um handler de ATS

Você (agente ou humano) vai criar um driver para um site de candidatura externo
(InHire, Greenhouse, Lever, Gupy, Workday, Ashby…) que o LinkedIn ou os scrapers
podem redirecionar. Siga os 6 passos **nesta ordem** — eles existem porque cada
site tem manias que só aparecem em vistoria.

Contrato: `ats_base.BaseATSHandler` · Registro: `@register` · Preenchimento: `ats_kernel`
(default, com salvaguardas anti-popup obrigatórias) · Dispatch: `ats_router.run_ats_flow`.

---

## 1. Reconhecimento (2–3 vagas reais)

Abra 2–3 vagas do site-alvo (pode usar `browser_snapshot`/CDP do Cursor, ou
`apply_via_browser` em dry-run) e anote:

- **Hosts exatos** das URLs de candidatura (o LinkedIn às vezes passa por
  `linkedin.com/jobs/apply/...?applicationId=` antes do redirect — o handler casa
  o host **final**; use `*.dominio.com` para subdomínios, ex.: `*.gupy.io`).
- **Estrutura**: página única ou wizard multi-step? Quantos passos? Botões de
  avanço dizem "Next", "Continue", "Avançar"?
- **Onde fica o formulário**: atrás de botão "Apply for the job"? Modal? Nova aba?
- **Submit**: texto exato do botão final ("Submit application", "Finalizar
  candidatura", "Continue registration"…).
- **Sucesso**: que frase/URL aparece após enviar (para `success_regex`).

## 2. Checklist de quirks (preencha no PR)

| Pergunta | Resposta | Onde tratar |
|---|---|---|
| Dropdown é `<select>` nativo ou widget custom (React/Chakra)? | | nativo = kernel; custom = `post_fill` |
| Telefone tem máscara/código de país? (BR: +55 antes do número) | | `post_fill` |
| Captcha/login existe? Em qual passo? | | `detect_obstacles`; **assisted-first** |
| Campos de diversidade/EULA/privacidade obrigatórios? | | `post_fill` |
| UI muda de idioma (PT/EN) por região? | | `pre_fill` |
| Wizard com passos custom (âncoras, não botões)? | | `advance_step` |
| Campos sem `<label>` (só placeholder)? | | alias extra no `form_field_rules` do painel |

## 3. Criar `ats_<site>.py` a partir de `ats_template.py`

```bash
cp ats_template.py ats_greenhouse.py
```

- Renomeie a classe (`GreenhouseHandler`), ajuste `name`, `hosts`, `auto_submit_capable=False`.
- **Regra dura**: todo seletor/extrâneo vive como constante no topo do arquivo com
  comentário obrigatório:

  ```python
  # evidência: https://boards.greenhouse.io/acme/jobs/1234 (2026-09-29, vistoria manual)
  CONSENT_CHECKBOX_SELECTOR = "#agree-terms, input[name='consent']"
  ```

  Sem evidência real (URL que você abriu), o seletor não entra — chute quebra
  calado semanas depois.
- Descomente `from ats_base import register` e o `@register` da classe.
- O kernel já trata: match rótulo→regra (`form_field_rules`), upload do resume
  (`mode=file`), carta (`mode=cover_letter`), cookie banners e perda de foco por
  popup (**não reimplemente isso no handler**). Você só escreve o que foge do padrão.
- Registre o módulo em `ats_router.py` → `_HANDLER_MODULES` (1 linha).

## 4. Testes obrigatórios (`tests/test_ats_<site>.py`)

Use a fake-page do padrão `tests/test_ats_kernel.py` (sem browser real):

- `can_handle` aceita as URLs reais coletadas no passo 1 e rejeita vizinhas;
- quirks do passo 2 têm teste (ex.: ordem do +55, checkbox de consent marcado);
- o handler importa sem colisão de registry (host/nome únicos → falha dura).

## 5. Smoke assistido (1 candidatura real)

No painel: vaga do site → Easy Apply. O robô deve preencher tudo e parar para
você resolver captcha/envio. **Só depois** de 1–2 smokes limpos você pode virar
`auto_submit_capable = True` — e mesmo assim, um site com captcha em etapa
final permanece assistido.

## 6. Definition of done

- [ ] `ats_<site>.py` com `@register` + 1 linha em `_HANDLER_MODULES`
- [ ] Testes verdes (`python3 -m unittest discover -s tests`)
- [ ] Toda constante de seletor tem `# evidência:`
- [ ] Smoke assistido executado (nota no PR com host e data)
- [ ] Esta tabela de quirks preenchida no PR (vira documentação viva)
- [ ] `auto_submit_capable` só é True com evidência de smoke aprovado

---

### O que o sistema faz por você (não duplique)

| Preocupação | Dono |
|---|---|
| Re-âncora de página quando popup abre nova aba | `ats_kernel.fill_page` + `ats_router._reanchor` |
| Fechar cookie-banner sem clicar submit por engano | `ats_kernel.dismiss_intruders` (lista negra) |
| Campo detached em re-render (retry + relocaliza) | `ats_kernel.safe_fill` |
| Match label→valor do candidato | `form_rules` via kernel |
| Wizard não avança com obrigatório faltando | `ats_kernel.fill_page` |
| Página fechada/crash vira `FillResult.error`, nunca exceção crua | kernel |
| Dispatch por URL e outcome `NO_HANDLER` | `ats_router` |
