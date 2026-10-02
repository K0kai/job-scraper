# job-scraper (estudo / fins educacionais)

Projeto **pessoal e educacional** para estudar, em ambiente local:

- painéis HTTP simples em Python (`http.server`)
- armazenamento local com SQLite
- integração com APIs de terceiros (quando o usuário configura as próprias chaves)
- parsing de PDF e fluxos de formulário web

**Não é um produto, serviço comercial nem ferramenta oferecida para uso em massa.**  
O código existe para aprendizado e experimentação **na sua máquina**.

## Aviso importante

- Respeite os **termos de uso** de qualquer site ou API que você consultar. Automação, scraping ou candidaturas automatizadas podem ser **proibidos** por essas plataformas.
- O autor **não incentiva** violar termos de serviço, contornar CAPTCHA/login nem abusar de contas.
- Qualquer uso além do estudo local é **por sua conta e risco**.
- Dados sensíveis (currículo, CPF, chaves de API, sessão de navegador) devem permanecer **apenas no seu computador** — nunca em commits.

Este repositório é fornecido **“como está”**, sem garantias. Não constitui aconselhamento jurídico.

## O que roda localmente

1. Interface web em `http://127.0.0.1:8765` (somente localhost por padrão).
2. Banco SQLite local (`jobs.db` — ignorado pelo Git).
3. Segredos (chaves de IA, tokens, senha SMTP) via **keyring** do sistema operacional, não no repositório.

## Setup (referência)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# dependências de browser, se for estudar esses módulos:
# playwright install
python app.py
```

Abra `http://127.0.0.1:8765`. Não exponha a porta na internet: o painel **não tem autenticação**.

## Privacidade e o que não versionar

Já ignorados pelo Git (entre outros):

- `jobs.db` e variantes
- PDFs em `resumes/`
- `linkedin_browser_profile/`, `copilot_uploads/`
- `docs/superpowers/` (notas internas de planejamento)
- `tests/` (suíte local)

Antes de qualquer push: `git status` e confirme que não há dados pessoais, currículos ou chaves.

## Licença

Uso educacional / estudo. Sem licença de redistribuição comercial definida.  
Se for bifurcar o código, mantenha os avisos deste README.
