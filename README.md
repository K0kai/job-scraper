# Odradek Scraper (estudo / fins educacionais)

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

## Deploy com Neon/PostgreSQL

- A aplicação usa SQLite local quando `DATABASE_URL` não está definida e PostgreSQL quando ela aponta para Neon.
- Execute no Neon SQL Editor o schema PostgreSQL completo (tabelas, índices e valores padrão) antes de iniciar a aplicação.
- Para copiar os dados locais para Neon, pare o serviço hospedado e rode `python migrate_sqlite_to_neon.py` no computador que contém `jobs.db`, com `DATABASE_URL` definido para a conexão Neon. O script importa tabelas e dados, preserva as configurações locais e substitui as regras iniciais/logs de primeiro boot pelas versões locais.
- Configure a mesma `DATABASE_URL` como variável secreta no Render. Nunca a salve no repositório.
- O Render não oferece o cofre de credenciais do sistema operacional usado pelo `keyring` local. Para salvar e alterar chaves pelo painel, configure **uma única vez** `SECRET_ENCRYPTION_KEY` em **Environment**. Gere uma chave Fernet localmente com `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, copie o resultado para o Render e mantenha uma cópia segura: sem essa mesma chave, os segredos salvos não podem ser descriptografados. As chaves de API são criptografadas com Fernet antes de serem gravadas como valores internos na tabela `settings` do Postgres. Variáveis como `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `APIFY_TOKEN` e `SMTP_PASSWORD` continuam aceitas como fallback, mas segredos cadastrados pelo painel no banco têm prioridade.
- Currículos e arquivos enviados ficam no sistema de arquivos, não no banco; configure um disco persistente no Render e defina `RESUMES_DIR` e `COPILOT_UPLOADS_DIR` para diretórios nesse disco. Copie os arquivos locais para lá antes de usar os registros migrados.
- O painel não tem autenticação. Não o exponha publicamente até adicionar proteção de acesso.
- O servidor não executa candidaturas por navegador. Em vagas com formulário, use **Abrir vaga + extensão** e importe manualmente seu perfil pelo botão da extensão; revise os campos e envie a candidatura no Chrome. A automação do servidor permanece limitada ao envio por e-mail SMTP. O endpoint `/api/autofill-rules` compartilha somente nomes e aliases de campos com a extensão.

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
