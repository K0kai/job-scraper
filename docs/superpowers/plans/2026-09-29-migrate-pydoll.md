# Migração Pydoll (motor de navegador) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trocar o motor de automação de navegador de Playwright para Pydoll (camada de compatibilidade `pydoll.playwright.sync_api`), com setting `browser_engine` configurável no painel e fallback automático, mantendo 100% dos testes verdes.

**Architecture:** Um helper central (`browser_engine.py`) resolve qual import usar (`pydoll.playwright.sync_api` ou `playwright.sync_api`) a partir do setting SQLite `browser_engine` (`pydoll`=padrão, `playwright`=fallback). `apply_channels.py`, `linkedin_apply.py` e `ats_inhire.py` passam a usar esse helper em vez de importar Playwright diretamente. O painel ganha um seletor "Motor de navegador" no form `/ai-settings`.

**Tech Stack:** Python 3.10+, pydoll-python 3.x (CDP direto, sem WebDriver), Playwright (fallback, já instalado), SQLite (settings), unittest.

## Global Constraints

- Python ≥ 3.10 (pydoll exige 3.10+; projeto roda 3.14).
- `pydoll-python>=3,<4` deve ser adicionado a `requirements.txt`; manter `playwright>=1.40,<2` como fallback.
- O caminho sincrono é `from pydoll.playwright.sync_api import sync_playwright` — a camada de compatibilidade exporta `sync_playwright` idêntico ao do Playwright real.
- **Nunca** auto-clicar Submit no LinkedIn (fluxo assistido permanece: robô preenche, usuário envia).
- Todo teste roda com `python3 -m unittest discover -s tests`.
- O import de navegador é **lazy** (dentro da função), nunca no topo do módulo — Playwright atual já faz isso e pydoll deve igual.
- Releases: commits frequentes em `master` direto (decisão do usuário), mensagens `feat:`/`fix:`.

---

### Task 1: Helper central `browser_engine.py` + setting `browser_engine`

**Files:**
- Create: `browser_engine.py`
- Modify: `app.py:90-137` (adicionar `"browser_engine": "pydoll"` ao `DEFAULT_SETTINGS`)
- Test: `tests/test_browser_engine.py`

**Interfaces:**
- Consumes: nada (novo módulo). `settings()`/`set_setting` de `app.py` para ler/escrever.
- Produces: `resolve_sync_playwright(cfg: dict[str, str]) -> ModuleType` (módulo com `sync_playwright`), `engine_name(cfg) -> str`, `engine_install_hint(cfg) -> str`.

- [ ] **Step 1: Write the failing test**

```python
import unittest
from unittest import mock

import browser_engine


class ResolveEngineTests(unittest.TestCase):
    def test_default_is_pydoll(self):
        with mock.patch.dict("sys.modules", clear=False):
            mod = browser_engine.resolve_sync_playwright({"browser_engine": "pydoll"})
            self.assertEqual(mod.__name__, "pydoll.playwright.sync_api")

    def test_playwright_fallback(self):
        mod = browser_engine.resolve_sync_playwright({"browser_engine": "playwright"})
        self.assertEqual(mod.__name__, "playwright.sync_api")

    def test_unknown_engine_falls_back_to_pydoll(self):
        mod = browser_engine.resolve_sync_playwright({"browser_engine": "selenium"})
        self.assertEqual(mod.__name__, "pydoll.playwright.sync_api")

    def test_missing_pydoll_returns_playwright(self):
        with mock.patch.dict("sys.modules", {"pydoll.playwright.sync_api": None}, clear=False):
            mod = browser_engine.resolve_sync_playwright({"browser_engine": "pydoll"})
            self.assertEqual(mod.__name__, "playwright.sync_api")

    def test_engine_name(self):
        self.assertEqual(browser_engine.engine_name({"browser_engine": "pydoll"}), "pydoll")
        self.assertEqual(browser_engine.engine_name({}), "pydoll")

    def test_install_hint_pydoll(self):
        hint = browser_engine.engine_install_hint({"browser_engine": "pydoll"})
        self.assertIn("pip install pydoll-python", hint)

    def test_install_hint_playwright(self):
        hint = browser_engine.engine_install_hint({"browser_engine": "playwright"})
        self.assertIn("playwright install", hint)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_browser_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'browser_engine'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Resolução do motor de navegador: Pydoll (padrão) ou Playwright (fallback).

Ambos expõem ``sync_playwright``; o Pydoll roda sobre CDP direto (sem
``navigator.webdriver``), o Playwright fica como fallback manual no painel.
"""
from __future__ import annotations

import importlib
from typing import Any

DEFAULT_ENGINE = "pydoll"
ENGINE_MODULES = {
    "pydoll": "pydoll.playwright.sync_api",
    "playwright": "playwright.sync_api",
}


def engine_name(cfg: dict[str, str] | None) -> str:
    name = (cfg or {}).get("browser_engine", DEFAULT_ENGINE)
    return name if name in ENGINE_MODULES else DEFAULT_ENGINE


def engine_module_name(cfg: dict[str, str] | None) -> str:
    return ENGINE_MODULES[engine_name(cfg)]


def resolve_sync_playwright(cfg: dict[str, str] | None) -> Any:
    """Retorna o módulo que expõe ``sync_playwright`` para o motor configurado.

    Se o motor escolhido (pydoll) não estiver instalado, cai para o Playwright
    real — nunca deixa o fluxo de candidatura sem motor.
    """
    wanted = engine_name(cfg)
    for candidate in (wanted, next(e for e in ENGINE_MODULES if e != wanted)):
        try:
            return importlib.import_module(ENGINE_MODULES[candidate])
        except ImportError:
            continue
    raise ImportError(
        "Nenhum motor de navegador disponível. Instale pydoll-python ou playwright."
    )


def engine_install_hint(cfg: dict[str, str] | None) -> str:
    name = engine_name(cfg)
    if name == "pydoll":
        return "pip install pydoll-python"
    return "pip install playwright && playwright install chromium"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_browser_engine.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Add setting default em `app.py`**

No `DEFAULT_SETTINGS` (perto de `"auto_apply"`), adicionar:
```python
    "browser_engine": "pydoll",
```

- [ ] **Step 6: Run full suite (regression)**

Run: `python3 -m unittest discover -s tests`
Expected: OK (24 + 7 novos = 31)

- [ ] **Step 7: Commit**

```bash
git add browser_engine.py tests/test_browser_engine.py app.py
git commit -m "feat: helper de motor de navegador pydoll/playwright + setting browser_engine"
```

---

### Task 2: Migrar `apply_via_browser` (formulários públicos) para o helper

**Files:**
- Modify: `apply_channels.py:260-263` (substituir `from playwright.sync_api import sync_playwright` por chamada ao helper)
- Test: `tests/test_browser_engine.py` (novo caso para a mensagem de fallback)

**Interfaces:**
- Consumes: `browser_engine.resolve_sync_playwright`, `browser_engine.engine_install_hint` (Task 1).
- Produces: `apply_via_browser` continua com a mesma assinatura; agora aceita `cfg["browser_engine"]` para escolher motor.

- [ ] **Step 1: Write the failing test (mensagem de erro com hint do motor)**

```python
class ApplyViaBrowserEngineTests(unittest.TestCase):
    def test_apply_via_browser_instal_hint_pydoll_present(self):
        import apply_channels
        # O import é lazy: não deve lançar aqui; apenas garante que o módulo
        # importa o helper sem erro quando o pydoll está instalado.
        self.assertTrue(hasattr(apply_channels, "apply_via_browser"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_browser_engine.py::ApplyViaBrowserEngineTests -v`
Expected: PASS (trivial — documenta que a função existe)

- [ ] **Step 3: Modify `apply_via_browser`**

Substituir o bloco de import em `apply_channels.py` (linhas ~260-263):

```python
    try:
        from browser_engine import engine_install_hint, resolve_sync_playwright

        module = resolve_sync_playwright(cfg)
        sync_playwright = module.sync_playwright
    except ImportError:
        return False, (
            "Motor de navegador indisponível. "
            f"Instale com: {engine_install_hint(cfg)}"
        )
```

E trocar a referência direta a `playwright` no corpo para usar a variável local `sync_playwright` (já é o caso — o código usa `with sync_playwright() as playwright:`).

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests/test_browser_engine.py -v && python3 -m unittest discover -s tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apply_channels.py tests/test_browser_engine.py
git commit -m "feat: apply_via_browser usa motor pydoll/playwright via helper"
```

---

### Task 3: Migrar `apply_via_linkedin` (Easy Apply assistido)

**Files:**
- Modify: `linkedin_apply.py:718-721` (import lazy)
- Test: `tests/test_browser_engine.py` (garantia de que `apply_via_linkedin` está importável e o helper é usado)

**Interfaces:**
- Consumes: `browser_engine.resolve_sync_playwright` (Task 1).
- Produces: `apply_via_linkedin` continua com a mesma assinatura; respeita `cfg["browser_engine"]`.

- [ ] **Step 1: Write the failing test**

```python
class LinkedInApplyEngineTests(unittest.TestCase):
    def test_linkedin_apply_module_importable(self):
        import linkedin_apply
        self.assertTrue(callable(linkedin_apply.apply_via_linkedin))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_browser_engine.py::LinkedInApplyEngineTests -v`
Expected: PASS (trivial)

- [ ] **Step 3: Modify `apply_via_linkedin`**

Substituir o bloco de import em `linkedin_apply.py` (linhas ~718-721):

```python
    try:
        from browser_engine import engine_install_hint, resolve_sync_playwright

        module = resolve_sync_playwright(cfg)
        sync_playwright = module.sync_playwright
    except ImportError:
        return False, "Motor de navegador indisponível na máquina."
```

O corpo já usa `with sync_playwright() as playwright:` — nada mais muda.

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests/test_browser_engine.py -v && python3 -m unittest discover -s tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add linkedin_apply.py tests/test_browser_engine.py
git commit -m "feat: apply_via_linkedin usa motor pydoll/playwright via helper"
```

---

### Task 4: Adicionar seletor `browser_engine` ao painel (`/ai-settings`)

**Files:**
- Modify: `app.py:2513` (form "Perfil, SMTP e automação" — adicionar `<select name="browser_engine">`)
- Modify: `app.py:3009-3019` (handler `/ai-settings` — nada muda; `save_settings` já persiste qualquer chave de `DEFAULT_SETTINGS`)
- Test: `tests/test_panel_live.py` (assert que o seletor aparece)

**Interfaces:**
- Consumes: `cfg` lido de `settings()` no `render_page` (já disponível como `cfg`).
- Produces: campo `browser_engine` no form; `save_settings(form)` persiste (já funciona porque `browser_engine` estará em `DEFAULT_SETTINGS`).

- [ ] **Step 1: Write the failing test**

```python
class BrowserEnginePanelTests(unittest.TestCase):
    def test_settings_panel_offers_engine_selector(self):
        from app import render_page
        html = render_page()
        self.assertIn('name="browser_engine"', html)
        self.assertIn("pydoll", html)
        self.assertIn("playwright", html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_browser_engine.py::BrowserEnginePanelTests -v`
Expected: FAIL — `assertIn('name="browser_engine"')` falha (seletor não existe ainda)

- [ ] **Step 3: Modify `render_page`**

No form "Perfil, SMTP e automação" (após o `<label>Modelo`), inserir:

```html
<label>Motor de navegador<select name="browser_engine">
<option value="pydoll" {'selected' if cfg.get('browser_engine','pydoll') == 'pydoll' else ''}>Pydoll (CDP, stealth — padrão)</option>
<option value="playwright" {'selected' if cfg.get('browser_engine','pydoll') == 'playwright' else ''}>Playwright (fallback)</option>
</select></label>
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests/test_browser_engine.py -v && python3 -m unittest discover -s tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_browser_engine.py
git commit -m "feat: seletor browser_engine (pydoll/playwright) no painel"
```

---

### Task 5: Adicionar `pydoll-python` ao `requirements.txt` + smoke test real

**Files:**
- Modify: `requirements.txt`
- Test: smoke test manual via `apply_via_browser` apontando para uma página de formulário simples.

**Interfaces:**
- Consumes: Task 2 (helper já em uso em `apply_via_browser`).
- Produces: dependência declarada; validação de que a camada de compatibilidade roda de verdade.

- [ ] **Step 1: Modify `requirements.txt`**

```
pydoll-python>=3,<4
```
(mantendo `playwright>=1.40,<2` como fallback)

- [ ] **Step 2: Write smoke test (automático, sem rede)**

```python
class PydollSmokeTests(unittest.TestCase):
    def test_sync_playwright_importable(self):
        from pydoll.playwright.sync_api import sync_playwright
        self.assertTrue(callable(sync_playwright))

    def test_browser_engine_resolves_pydoll_module(self):
        import browser_engine
        mod = browser_engine.resolve_sync_playwright({"browser_engine": "pydoll"})
        self.assertTrue(callable(mod.sync_playwright))
```

- [ ] **Step 3: Run tests**

Run: `python3 -m unittest tests/test_browser_engine.py -v`
Expected: PASS

- [ ] **Step 4: Smoke manual (opcional, exige Chrome)**

```bash
python3 - <<'PY'
import browser_engine
m = browser_engine.resolve_sync_playwright({"browser_engine": "pydoll"})
print("motor:", m.__name__)
PY
```
Expected: `motor: pydoll.playwright.sync_api`

- [ ] **Step 5: Commit**

```bash
git add requirements.txt tests/test_browser_engine.py
git commit -m "chore: declara pydoll-python e smoke test do motor pydoll"
```

---

### Task 6: Atualizar hint do painel (Playwright → pydoll) e rodada final

**Files:**
- Modify: `app.py` hint text "Regras de formulário (Playwright)" → "Regras de formulário (navegador)"
- Modify: `apply_channels.py` mensagem "Falha no Playwright:" → "Falha no navegador:"
- Test: suíte completa.

**Interfaces:**
- Consumes: todas as tasks anteriores.
- Produces: código limpo de referências diretas a Playwright nas mensagens ao usuário.

- [ ] **Step 1: Write test (mensagens neutras)**

```python
class NeutralWordingTests(unittest.TestCase):
    def test_no_hardcoded_playwright_mentions_in_ui(self):
        import app
        html = app.render_page()
        # O seletor ainda menciona "Playwright (fallback)" como opção — permitido.
        self.assertIn("Regras de formulário (navegador)", html)

    def test_apply_via_browser_error_is_engine_agnostic(self):
        import apply_channels
        src = inspect.getsource(apply_channels.apply_via_browser)
        self.assertNotIn("Falha no Playwright", src)
```

- [ ] **Step 2: Run test (deve falhar primeiro)**

Run: `python3 -m unittest tests/test_browser_engine.py::NeutralWordingTests -v`
Expected: FAIL

- [ ] **Step 3: Apply neutral wording**

- `app.py`: trocar `Regras de formulário (Playwright)` → `Regras de formulário (navegador)`.
- `apply_channels.py`: trocar `f"Falha no Playwright: {exc}"` → `f"Falha no navegador: {exc}"` e "Formulário enviado via Playwright" → "Formulário enviado via navegador".

- [ ] **Step 4: Run full suite**

Run: `python3 -m unittest discover -s tests`
Expected: OK (todos)

- [ ] **Step 5: Commit**

```bash
git add app.py apply_channels.py tests/test_browser_engine.py
git commit -m "refactor: mensagens de usuário neutras (navegador) em vez de Playwright"
```