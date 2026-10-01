# Salary period + review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Detect salary period, propose FX-normalized value, always AI-review salary fills with offshore policy; ask human only when needed.

**Architecture:** Pure `salary_policy.py` for period/normalize/propose; `salary_review.py` for LLM JSON review; wire into salary fill paths; align copiloto prompt.

**Tech Stack:** Python, existing `form_rules.salary_for` / `fx_rates`, `ai_client.call_ai_text`, unittest.

## Global Constraints

- No external salary-band API
- Review failure must not abort application
- Floor = BR expectation converted; prefer panel USD remote anchor

---

### Task 1: `salary_policy.py`

- [ ] Failing tests for period detect + normalize + propose
- [ ] Implement module

### Task 2: `salary_review.py`

- [ ] Failing tests for parse ok/adjust/ask + unavailable fallback
- [ ] Implement review call

### Task 3: Wire fill + copiloto prompt

- [ ] Hook salary mode fill
- [ ] Update `ats_copilot` prompt policy text
- [ ] Tests / smoke
