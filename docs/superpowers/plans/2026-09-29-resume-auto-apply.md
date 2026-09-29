# Resume Auto-Apply Implementation Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Upload PDFs once-analyzed, match jobs against stored resume analysis, auto-apply via SMTP then Playwright.

**Architecture:** Keep HTTP/UI/collector in `app.py`; add `resume_pipeline.py` (PDF + AI analysis), `form_rules.py` (rules CRUD/match), `apply_channels.py` (SMTP + Playwright + open questions). Wire `process_auto_job` into the collector when `auto_apply=1`.

**Tech Stack:** Python 3 stdlib HTTP server, SQLite, pypdf, playwright, keyring, Gemini/OpenAI APIs.

## Global Constraints

- Localhost only (`127.0.0.1`)
- Never invent candidate facts beyond resume analysis + configured profile
- No login/CAPTCHA bypass
- Skip job (`blocked`) if open question needs AI and AI is unavailable
- Secrets only in keyring
- Cross-platform paths via `os.path` (fix Windows-only `\\` ROOT)

---

### Task 1: Schema, settings, default form rules

**Files:**
- Modify: `app.py` (`initialize`, `DEFAULT_SETTINGS`, migrations)
- Create: `form_rules.py`

- [ ] Add tables `resumes`, `form_field_rules`, `applications`, `form_answers`
- [ ] Migrate `ai_decisions` columns
- [ ] Seed default rules (name, email, phone, linkedin, city, salary select, cover_letter, file)
- [ ] Add SMTP + profile settings keys
- [ ] Verify: `python -c "import app; app.initialize(); print('ok')"`

### Task 2: Resume upload + one-shot AI analysis

**Files:**
- Create: `resume_pipeline.py`
- Modify: `app.py` (route `/upload-resume`, UI section)

- [ ] Extract PDF text with pypdf
- [ ] Hash file; skip AI if unchanged
- [ ] Persist analysis; expose helpers `get_resume(lang)`, `resume_summaries()`
- [ ] Multipart parse in handler
- [ ] Verify: unit test hash skip logic without network

### Task 3: Match + apply orchestration

**Files:**
- Modify: `app.py` (`ai_assess_job`, `process_auto_job`, `Collector._run_once`)
- Create: `apply_channels.py`

- [ ] Load summaries from `resumes` table
- [ ] Always generate cover letter when qualifying for apply
- [ ] Try email then browser; record `applications`
- [ ] Call auto-process after each collect cycle when `auto_apply=1`
- [ ] Cap with `maximum_applications_per_run`

### Task 4: SMTP + email extraction

**Files:**
- Modify: `apply_channels.py`, `app.py` (SMTP UI routes)

- [ ] Extract emails from job description (regex)
- [ ] Send MIME multipart with PDF + letter body
- [ ] `/smtp-settings`, `/smtp-test`

### Task 5: Playwright form fill + open questions

**Files:**
- Modify: `apply_channels.py`, `form_rules.py`

- [ ] Map fields via aliases; select fuzzy option match
- [ ] Open questions → AI grounded in analysis; on AI failure → blocked skip
- [ ] No login walls: detect and abort

### Task 6: Panel UI polish + requirements

**Files:**
- Modify: `app.py` `render_page`, `requirements.txt`, `.gitignore`

- [ ] Sections for resumes, profile rules, SMTP
- [ ] Show resume analysis preview and apply notes
- [ ] Add `pypdf`, `playwright`; ignore `resumes/*.pdf`
