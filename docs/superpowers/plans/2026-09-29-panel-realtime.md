# Panel Realtime Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Atualizar ao vivo o status dos currículos e executar ações de processo via `fetch` sem reload da página.

**Architecture:** Ampliar `GET /live` com HTML só da zona de status do currículo (sem `<input type="file">`). Rotas de processo respondem JSON quando o cliente pede AJAX; o JS intercepta esses forms, mostra notice em `#panel-notice` e chama `refresh()`. Settings continuam com POST clássico.

**Tech Stack:** Python 3 stdlib (`BaseHTTPRequestHandler`), SQLite, JS vanilla inline em `app.py`. Sem libs novas.

**Spec:** `docs/superpowers/specs/2026-09-29-panel-realtime-design.md`

## Global Constraints

- Localhost only (`127.0.0.1`)
- Sem SSE/WebSocket/SPA
- Sem libs front-end novas
- Settings/IA/SMTP/regras/job-status/notes: POST clássico (fora do AJAX)
- Polling ~1500 ms; não apagar arquivo já escolhido no input de PDF
- Caminhos cross-platform via `os.path`

## File map

| File | Responsibility |
|------|----------------|
| `app.py` | HTML cards currículo, `live_payload`, helpers JSON/notice, handlers POST, JS inline |
| `tests/test_panel_live.py` | Unit tests de helpers (status HTML, wants_json, live keys) |

---

### Task 1: Extrair HTML de status do currículo + incluir no `/live`

**Files:**
- Modify: `app.py` (`resume_panels_html`, `live_payload`, ~1382–1470 e ~1364–1378)
- Create: `tests/test_panel_live.py`

**Interfaces:**
- Produces: `resume_status_html_for(lang: str) -> str`
- Produces: `resume_status_payload() -> dict[str, str]` com chaves `"pt"` e `"en"`
- Produces: `live_payload()["resume_status_html"]` = esse dict

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_panel_live.py
import unittest

import app


class ResumeStatusLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.initialize()

    def test_status_html_has_no_file_input(self):
        html = app.resume_status_html_for("pt")
        self.assertNotIn('type="file"', html)
        self.assertNotIn("force_reanalyze", html)

    def test_live_payload_includes_resume_status(self):
        payload = app.live_payload()
        self.assertIn("resume_status_html", payload)
        self.assertIn("pt", payload["resume_status_html"])
        self.assertIn("en", payload["resume_status_html"])
        for lang in ("pt", "en"):
            self.assertNotIn('type="file"', payload["resume_status_html"][lang])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python -m unittest tests.test_panel_live -v`  
Expected: FAIL (`resume_status_html_for` ausente ou `resume_status_html` não no payload)

- [ ] **Step 3: Implement helpers and split panel HTML**

Em `app.py`, extrair a lógica de status atual de `resume_panels_html` para:

```python
def resume_status_html_for(lang: str) -> str:
    """HTML só da zona de status (badge/dossiê/reanalisar). Sem input de arquivo."""
    with connect() as db:
        row = get_resume(db, lang)
    if not row:
        return (
            '<div class="analysis-badge analysis-none">Sem PDF</div>'
            '<p class="hint">Nenhum currículo enviado. Escolha um PDF e clique em enviar para a IA analisar.</p>'
        )
    # ... mover o bloco else atual de resume_panels_html (badge, message, reanalyze, dossier) ...
    return status  # string já montada


def resume_status_payload() -> dict[str, str]:
    return {"pt": resume_status_html_for("pt"), "en": resume_status_html_for("en")}
```

Refatorar `resume_panels_html()` para:

```python
blocks.append(
    f'<div class="resume-card"><h3 style="margin:0 0 8px;font-size:15px">Currículo {label}</h3>'
    f'<form method="post" action="/upload-resume" enctype="multipart/form-data" class="js-process-form">'
    f'<input type="hidden" name="language" value="{lang}">'
    f'<label>Selecionar PDF<input type="file" name="file" accept="application/pdf" required></label>'
    f'<label style="margin-top:8px;font-weight:500">'
    f'<input type="checkbox" name="force_reanalyze" value="1" style="width:auto;margin-right:6px">'
    f'Forçar nova análise mesmo se o arquivo for idêntico</label>'
    f'<button style="margin-top:8px">Enviar e analisar</button></form>'
    f'<div id="resume-status-{lang}">{resume_status_html_for(lang)}</div></div>'
)
```

No form de reanalisar (dentro do status), adicionar `class="js-process-form"`.

Atualizar `live_payload()`:

```python
def live_payload() -> dict:
    status = collector.snapshot()
    counts, jobs, runs = load_dashboard()
    collecting = status["state"] in {"running", "stopping"}
    return {
        "state": status["state"],
        "state_label": state_label_for(status["state"]),
        "message": status["message"],
        "stats_html": stats_html(counts),
        "jobs_html": job_rows_html(jobs, collecting),
        "history_html": history_html(runs),
        "logs_html": logs_html(),
        "queue_html": queue_html(),
        "worth_html": worth_html(),
        "resume_status_html": resume_status_payload(),
    }
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m unittest tests.test_panel_live -v`  
Expected: OK

- [ ] **Step 5: Commit** (só se o usuário pedir commit nesta sessão)

```bash
git add app.py tests/test_panel_live.py
git commit -m "$(cat <<'EOF'
Split resume status HTML for live panel updates.

EOF
)"
```

---

### Task 2: Resposta JSON vs HTML nas rotas de processo

**Files:**
- Modify: `app.py` (`Handler.redirect` / novo helper, `do_POST` das rotas de processo)
- Modify: `tests/test_panel_live.py`

**Interfaces:**
- Produces: `request_wants_json(headers) -> bool`
- Produces: `Handler.respond_notice(notice: str, notice_kind: str = "success", *, ok: bool = True, status: int = 200) -> None`
- Consumes: rotas listadas na spec

- [ ] **Step 1: Write the failing tests**

```python
class WantsJsonTests(unittest.TestCase):
    def test_accept_json(self):
        self.assertTrue(app.request_wants_json({"Accept": "application/json"}))
        self.assertTrue(app.request_wants_json({"X-Requested-With": "fetch"}))
        self.assertFalse(app.request_wants_json({"Accept": "text/html"}))
```

- [ ] **Step 2: Run — expect FAIL** (`request_wants_json` ausente)

- [ ] **Step 3: Implement helpers**

```python
def request_wants_json(headers) -> bool:
    accept = (headers.get("Accept") or "").casefold()
    if "application/json" in accept:
        return True
    return (headers.get("X-Requested-With") or "").casefold() == "fetch"
```

No `Handler`:

```python
def wants_json(self) -> bool:
    return request_wants_json(self.headers)

def respond_notice(self, notice: str = "", notice_kind: str = "success", *, ok: bool = True, status: int = 200) -> None:
    if self.wants_json():
        self.send_json({"ok": ok, "notice": notice, "kind": notice_kind}, status=status)
        return
    self.redirect(notice, notice_kind=notice_kind)
```

Trocar **todas** as chamadas `self.redirect(...)` nestas rotas por `self.respond_notice(...)`:

- `/upload-resume` (sucesso e except)
- `/reanalyze-resume` (todos os early-returns e sucesso)
- `/start`, `/stop`
- `/queue-cancel`, `/queue-retry`, `/queue-clear`
- `/clear-logs`

Nas falhas de negócio (ex.: cancel falhou), usar `ok=False` com HTTP 200 para o JS mostrar notice sem rejeitar o `fetch`.

Marcar forms de processo no HTML gerado:

- start/stop forms: `class="js-process-form"`
- clear-logs form: `class="js-process-form"`
- queue action forms em `queue_html`: `class="js-process-form"`

- [ ] **Step 4: Smoke unit**

```bash
python -c "
import app
assert app.request_wants_json({'Accept':'application/json'})
assert not app.request_wants_json({'Accept':'text/html'})
app.initialize()
print('ok')
"
python -m unittest tests.test_panel_live -v
```

Expected: `ok` + testes PASS

- [ ] **Step 5: Commit** (só se pedido)

```bash
git add app.py tests/test_panel_live.py
git commit -m "$(cat <<'EOF'
Return JSON notices for process POSTs when client asks.

EOF
)"
```

---

### Task 3: JS — notice ao vivo, refresh de currículo, intercept de forms

**Files:**
- Modify: `app.py` (`render_page` — markup do notice + script inline ~1525–1587)

**Interfaces:**
- Consumes: `live_payload.resume_status_html`
- Consumes: `POST` JSON `{ok, notice, kind}` das rotas de processo
- Produces: banner `#panel-notice`; updates `#resume-status-pt` / `#resume-status-en`

- [ ] **Step 1: Markup do notice**

Trocar o `notice_html` estático por container sempre presente:

```python
notice_html = (
    f'<div id="panel-notice" class="{notice_class}" {"hidden" if not notice else ""}>'
    f'{esc(notice) if notice else ""}</div>'
)
```

Se `notice` vazio no primeiro render: `class="notice"` + `hidden` (sem texto).

- [ ] **Step 2: Estender `refresh()` no script**

Dentro do `.then(function (data) { ... })` existente, adicionar:

```javascript
var resumePt = document.getElementById("resume-status-pt");
var resumeEn = document.getElementById("resume-status-en");
if (data.resume_status_html) {
  if (resumePt && data.resume_status_html.pt && resumePt.innerHTML !== data.resume_status_html.pt) {
    resumePt.innerHTML = data.resume_status_html.pt;
  }
  if (resumeEn && data.resume_status_html.en && resumeEn.innerHTML !== data.resume_status_html.en) {
    resumeEn.innerHTML = data.resume_status_html.en;
  }
}
```

- [ ] **Step 3: Helpers de notice + intercept de forms**

No mesmo IIFE do script, adicionar:

```javascript
var noticeTimer = null;
var PROCESS_PATHS = {
  "/upload-resume": 1,
  "/reanalyze-resume": 1,
  "/start": 1,
  "/stop": 1,
  "/queue-cancel": 1,
  "/queue-retry": 1,
  "/queue-clear": 1,
  "/clear-logs": 1
};
function noticeClass(kind) {
  return {
    success: "notice notice-ok",
    info: "notice notice-info",
    error: "notice notice-error",
    warning: "notice notice-warn"
  }[kind] || "notice";
}
function showNotice(text, kind) {
  var el = document.getElementById("panel-notice");
  if (!el) return;
  el.className = noticeClass(kind || "info");
  el.textContent = text || "";
  el.hidden = !text;
  if (noticeTimer) clearTimeout(noticeTimer);
  if (text) {
    noticeTimer = setTimeout(function () { el.hidden = true; }, 6000);
  }
}
document.addEventListener("submit", function (ev) {
  var form = ev.target;
  if (!(form instanceof HTMLFormElement)) return;
  var action = form.getAttribute("action") || "";
  var path = action.split("?")[0];
  if (!PROCESS_PATHS[path]) return;
  ev.preventDefault();
  var btn = form.querySelector('button[type="submit"], button:not([type])');
  if (btn) btn.disabled = true;
  var opts = {
    method: "POST",
    body: new FormData(form),
    headers: { Accept: "application/json", "X-Requested-With": "fetch" },
    credentials: "same-origin"
  };
  fetch(path, opts)
    .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
    .then(function (res) {
      var data = res.data || {};
      showNotice(data.notice || (data.ok ? "OK" : "Falha"), data.kind || (data.ok ? "success" : "error"));
      if (path === "/upload-resume" && data.ok !== false) {
        var fileInput = form.querySelector('input[type="file"]');
        if (fileInput) fileInput.value = "";
      }
      refresh();
    })
    .catch(function () {
      showNotice("Falha de comunicação com o painel.", "error");
    })
    .then(function () {
      if (btn) btn.disabled = false;
    });
});
```

Notas:
- Event delegation no `document` cobre forms de fila recriados pelo live
- Manter sticky-bottom dos logs como está
- Não alterar tabs/`localStorage`

- [ ] **Step 4: Verificação**

```bash
python -c "
import app
app.initialize()
html = app.render_page()
assert 'id=\"panel-notice\"' in html
assert 'id=\"resume-status-pt\"' in html
assert 'id=\"resume-status-en\"' in html
assert 'resume_status_html' in app.live_payload()
print('ok')
"
python -m unittest tests.test_panel_live -v
```

Expected: `ok` + PASS

Checklist manual (app em `127.0.0.1:8765`):
1. Escolher PDF e esperar ~3s de poll → arquivo ainda selecionado
2. Enviar currículo → notice no topo, sem F5; badge via live
3. Reanalisar → notice + pending sem F5
4. Cancelar job na aba Filas → sem reload
5. Salvar preferências → ainda dá reload (esperado)

- [ ] **Step 5: Commit** (só se pedido)

```bash
git add app.py tests/test_panel_live.py docs/superpowers/specs/2026-09-29-panel-realtime-design.md docs/superpowers/plans/2026-09-29-panel-realtime.md
git commit -m "$(cat <<'EOF'
Make process actions and resume status update without full reload.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| `resume_status_html` no `/live` | Task 1 |
| Zona upload vs status separadas | Task 1 |
| Não incluir file input no live | Task 1 |
| JSON notice nas rotas de processo | Task 2 |
| Settings fora do AJAX | Task 2 (não alterar) |
| JS fetch + notice + refresh | Task 3 |
| Limpar file após upload ok | Task 3 |
| Notice some ~6s | Task 3 |
| Poll 1,5s / visibility refresh | já existe; Task 3 só estende |
| Sem SSE/libs | global |

## Self-review

- Sem placeholders TBD
- Rotas alinhadas: `/queue-cancel`, `/queue-retry`, `/queue-clear`
- `/job-status` e `/notes` deliberadamente fora
- Nomes estáveis: `resume_status_html_for`, `resume_status_payload`, `request_wants_json`, `respond_notice`
