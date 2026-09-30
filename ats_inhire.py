# InHire (inhire.app) ATS — handler da arquitetura ats_base.
# NUNCA resolve captcha nem clica "Continue registration" (final) — assisted-first.
# quirks mapeados: dropdowns React (aria-label="Dropdown select"), telefone BR com
# +55 ANTES do número, país "Brazil (BR)" em UI inglesa, diversidade PCD, EULA.
from __future__ import annotations

import logging
import random
import re
import time

from ats_base import BaseATSHandler, FillResult, register

LOG = logging.getLogger("job-scraper")

SUCCESS_RE = re.compile(
    r"candidatura\s+enviada|application\s+sent|obrigad[oa]|recebemos\s+sua|"
    r"inscri[cç][aã]o\s+enviada|sucesso|registration\s+sent|continue\s+registration",
    re.I,
)

# evidência: https://acme.inhire.app jobs reais (sessão Easy Apply, 2026-09)
NAME_FIELD_SELECTOR = "#name, input[name='name']"
CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha", "cf-challenge")


def _pause(lo: float = 0.3, hi: float = 0.9) -> None:
    time.sleep(random.uniform(lo, hi))


def _linkedin_profile_value(cfg: dict[str, str]) -> str:
    raw = (cfg.get("candidate_linkedin") or "").strip()
    if not raw:
        return ""
    if "linkedin.com" not in raw.casefold():
        return f"https://www.linkedin.com/in/{raw.lstrip('/')}"
    if not raw.startswith("http"):
        return "https://" + raw
    return raw


def _local_phone_digits(cfg: dict[str, str]) -> str:
    """National number only — dial code is chosen in the phone-country dropdown first."""
    digits = re.sub(r"\D+", "", cfg.get("candidate_phone") or "")
    if digits.startswith("55") and len(digits) >= 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) > 10:
        digits = digits.lstrip("0")
    return digits


def _prefer_english_ui(page) -> None:
    """Country list uses 'Brazil (BR)' in English; prefer that for reliable matching."""
    try:
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            try:
                options = sel.locator("option").all_inner_texts()
            except Exception:
                continue
            joined = " ".join(options).casefold()
            if "english" in joined or any(o.strip().casefold() == "en" for o in options):
                try:
                    sel.select_option(value="en")
                except Exception:
                    try:
                        sel.select_option(label="English")
                    except Exception:
                        continue
                _pause(1.0, 1.8)
                return
    except Exception as exc:
        LOG.info("InHire language switch skipped: %s", exc)


def _select_country_brazil(page) -> bool:
    """Type/select Brazil (not Brasil) — works on English UI as 'Brazil (BR)'."""
    if _select_react_dropdown(
        page,
        matcher=r"Select a country|Country of origin|Selecione o pa[ií]s",
        option_query="Brazil",
        option_regex=r"Brazil\s*\(BR\)|^Brazil$",
    ):
        return True
    # Open any country placeholder and pick Brazil (BR) from filtered list.
    try:
        dd = page.locator('[aria-label="Dropdown select"]').filter(
            has_text=re.compile(r"country|pa[ií]s|Select a country|Selecione o", re.I)
        )
        if not dd.count():
            return False
        dd.first.click(force=True)
        _pause(0.2, 0.4)
        page.keyboard.type("Brazil", delay=30)
        _pause(0.5, 0.9)
        opt = page.locator(
            ".react-dropdown-select-dropdown button, "
            ".react-dropdown-select-dropdown [role='option']"
        ).filter(has_text=re.compile(r"Brazil", re.I))
        if opt.count():
            opt.first.click(force=True)
            _pause(0.4, 0.8)
            return True
        page.keyboard.press("Enter")
        _pause(0.4, 0.8)
        return True
    except Exception as exc:
        LOG.warning("InHire country Brazil failed: %s", exc)
        return False


def _select_react_dropdown(
    page,
    *,
    matcher: str,
    option_query: str,
    option_regex: str | None = None,
    timeout: float = 8000,
    index: int | None = None,
) -> bool:
    """Open a react-dropdown-select, type a query, pick an option (force-click OK)."""
    if index is not None:
        dd = page.locator('[aria-label="Dropdown select"]').nth(index)
    else:
        dd = page.locator('[aria-label="Dropdown select"]').filter(has_text=re.compile(matcher, re.I))
        if not dd.count():
            all_dd = page.locator('[aria-label="Dropdown select"]')
            chosen = None
            for i in range(all_dd.count()):
                try:
                    if not all_dd.nth(i).is_visible(timeout=300):
                        continue
                    text = all_dd.nth(i).inner_text(timeout=500) or ""
                except Exception:
                    continue
                if re.search(matcher, text, re.I):
                    chosen = all_dd.nth(i)
                    break
            if chosen is None:
                return False
            dd = chosen
        else:
            # Prefer a visible match.
            picked = None
            for i in range(dd.count()):
                try:
                    if dd.nth(i).is_visible(timeout=300):
                        picked = dd.nth(i)
                        break
                except Exception:
                    continue
            dd = picked or dd.first
    try:
        dd.scroll_into_view_if_needed(timeout=3000)
        dd.click(timeout=timeout, force=True)
        _pause(0.2, 0.45)
        page.keyboard.type(option_query, delay=random.randint(20, 45))
        _pause(0.45, 0.9)
        opt_re = re.compile(option_regex or re.escape(option_query), re.I)
        opt = page.locator(
            ".react-dropdown-select-dropdown button, "
            ".react-dropdown-select-dropdown [role='option']"
        ).filter(has_text=opt_re)
        if opt.count():
            opt.first.click(timeout=timeout, force=True)
        else:
            page.keyboard.press("Enter")
        _pause(0.35, 0.7)
        return True
    except Exception as exc:
        LOG.warning("InHire dropdown (%s → %s) failed: %s", matcher, option_query, exc)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _select_phone_country_code(page, *, dial: str = "+55") -> bool:
    """Must run BEFORE typing the phone number."""
    try:
        phone_dd = page.locator('[aria-label="Dropdown select"]').nth(0)
        phone_dd.scroll_into_view_if_needed(timeout=3000)
        phone_dd.click(timeout=5000, force=True)
        _pause(0.2, 0.4)
        page.keyboard.type(dial, delay=random.randint(25, 50))
        _pause(0.45, 0.8)
        opt = page.locator(
            ".react-dropdown-select-dropdown button, "
            ".react-dropdown-select-dropdown [role='option']"
        ).filter(has_text=re.compile(re.escape(dial)))
        if opt.count():
            # Option may be aria-selected/disabled — Enter or force click.
            try:
                opt.first.click(timeout=2000, force=True)
            except Exception:
                page.keyboard.press("Enter")
        else:
            page.keyboard.press("Enter")
        _pause(0.35, 0.7)
        return True
    except Exception as exc:
        LOG.warning("InHire phone country code failed: %s", exc)
        return False


def _click_visible_choice(page, texts: tuple[str, ...], *, nth: int = 0) -> bool:
    for text in texts:
        loc = page.get_by_text(text, exact=True)
        try:
            visible = []
            for i in range(loc.count()):
                if loc.nth(i).is_visible(timeout=200):
                    visible.append(loc.nth(i))
            if not visible:
                continue
            idx = min(nth, len(visible) - 1)
            visible[idx].click(timeout=4000)
            _pause(0.2, 0.5)
            return True
        except Exception:
            continue
    return False


def _fill_if_present(page, selector: str, value: str) -> None:
    if not value:
        return
    loc = page.locator(selector).first
    try:
        if not loc.count():
            return
        loc.scroll_into_view_if_needed(timeout=2000)
        loc.click(timeout=3000)
        loc.fill(value, timeout=5000)
        _pause(0.2, 0.5)
    except Exception as exc:
        LOG.warning("InHire fill %s failed: %s", selector, exc)


def _fill_diversity_step(page, *, cfg: dict[str, str]) -> None:
    """Step 2: PCD Yes/No + privacy. Does not click final Continue (captcha)."""
    # Navigate to diversity if Next is available.
    next_btn = page.get_by_role("button", name=re.compile(r"^(Next|Avançar|Continue)$", re.I))
    try:
        if next_btn.count() and next_btn.first.is_enabled(timeout=1500):
            next_btn.first.click(timeout=5000)
            _pause(1.0, 1.8)
    except Exception:
        try:
            page.get_by_role("button", name=re.compile(r"Diversity|Diversidade", re.I)).first.click(force=True)
            _pause(0.8, 1.4)
        except Exception:
            pass

    pcd_yes = (cfg.get("inhire_pcd") or cfg.get("candidate_pcd") or "0").strip() in {"1", "yes", "sim", "true"}
    pcd_label = "Yes" if pcd_yes else "No"
    # PT UI fallbacks
    pcd_query = pcd_label
    pcd_alt = "Sim" if pcd_yes else "Não"

    opened = _select_react_dropdown(
        page,
        matcher=r"Select one of the options|Selecione uma das opções",
        option_query=pcd_query,
        option_regex=rf"^{re.escape(pcd_query)}$|^{re.escape(pcd_alt)}$",
    )
    if not opened:
        # Click option list without filter text (already open / different placeholder).
        try:
            dd = page.locator('[aria-label="Dropdown select"]').filter(
                has_text=re.compile(r"Select one|Selecione uma|Yes|No|Sim|Não", re.I)
            )
            if dd.count():
                dd.first.click(force=True)
                _pause(0.3, 0.6)
                opt = page.locator(".react-dropdown-select-dropdown button").filter(
                    has_text=re.compile(rf"^{re.escape(pcd_query)}$|^{re.escape(pcd_alt)}$", re.I)
                )
                if opt.count():
                    opt.first.click(force=True)
        except Exception as exc:
            LOG.warning("InHire PCD dropdown failed: %s", exc)

    # Privacy agreement on diversity step.
    try:
        box = page.locator("#privacyPolicy, input[name='privacyPolicy']")
        if box.count():
            if not box.first.is_checked():
                box.first.check(force=True)
                _pause(0.2, 0.4)
    except Exception as exc:
        LOG.warning("InHire privacy checkbox failed: %s", exc)


@register
class InHireHandler(BaseATSHandler):
    """Primeiro handler concreto da arquitetura — assistido (captcha no final)."""

    name = "inhire"
    hosts = ("inhire.app", "*.inhire.app")
    auto_submit_capable = False
    success_regex = SUCCESS_RE

    @classmethod
    def can_handle(cls, url: str) -> bool:
        # mantém o comportamento legado: host OU substring em URLs estranhas
        return super().can_handle(url) or "inhire.app" in (url or "").casefold()

    def wait_ready(self, page, timeout_ms: int = 15000) -> bool:
        try:
            page.locator(NAME_FIELD_SELECTOR).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def detect_obstacles(self, page) -> list[str]:
        try:
            html = page.content()[:200000]
        except Exception:
            return ["pagina-inacessivel"]
        low = html.casefold()
        if any(marker in low for marker in CAPTCHA_MARKERS):
            return ["captcha"]
        return []

    def fill(self, page, ctx) -> FillResult:
        """Sequência manual comprovada em campo (dropdowns React, ordem do telefone).

        O kernel genérico não alcança os widgets custom do InHire; sobrescrever
        é o caso previsto pelo contrato. Nunca lança — devolve FillResult.
        """
        cfg = ctx.cfg
        resume_path = ctx.resume_path
        filled: list[str] = []
        missing: list[str] = []
        try:
            _prefer_english_ui(page)

            for label in ("Candidatar-se para a vaga", "Candidatar-se", "Candidatar", "Apply", "Apply for the job"):
                btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
                try:
                    if btn.count() and btn.first.is_visible(timeout=400):
                        btn.first.click(timeout=4000)
                        _pause(0.8, 1.4)
                        break
                except Exception:
                    continue

            if not self.wait_ready(page, timeout_ms=15000):
                return FillResult(ok=False, filled=filled, missing=["form"],
                                  error="Formulario InHire nao apareceu (campo nome).")

            name = (cfg.get("candidate_name") or "").strip()
            email = (cfg.get("candidate_email") or "").strip()
            phone = _local_phone_digits(cfg)
            city = (cfg.get("candidate_city") or "").strip() or "Belo Horizonte"
            city = city.split(",")[0].strip()
            linkedin = _linkedin_profile_value(cfg)
            salary = (cfg.get("linkedin_salary_expectation") or cfg.get("salary_expectation") or ctx.salary or "").strip()

            _fill_if_present(page, "#name, input[name='name']", name)
            filled.append("nome")
            _fill_if_present(page, "#email, input[name='email']", email)
            filled.append("email")

            # Area code BEFORE the number.
            _select_phone_country_code(page, dial="+55")
            _fill_if_present(page, "#phone, input[name='phone']", phone)
            filled.append("telefone")

            _fill_if_present(page, "#linkedinUsername, input[name='linkedinUsername']", linkedin)
            filled.append("linkedin")

            # Country of origin — always type/select "Brazil" (English list).
            if _select_country_brazil(page):
                filled.append("pais")
            else:
                missing.append("pais")

            # City dropdown enables after country.
            _pause(0.5, 1.0)
            if _select_react_dropdown(
                page,
                matcher=r"Enter your city|Informe sua cidade|City|cidade",
                option_query=city,
                option_regex=re.escape(city),
            ):
                filled.append("cidade")
            else:
                # Only touch free-text city if enabled.
                try:
                    city_input = page.locator("#district, input[name='district'], input[name='districtBr']").first
                    if city_input.count() and city_input.is_enabled(timeout=800):
                        city_input.fill(city)
                        filled.append("cidade")
                    else:
                        missing.append("cidade")
                except Exception:
                    missing.append("cidade")

            if salary:
                _fill_if_present(page, "#salaryExpectation, input[name='salaryExpectation']", salary)
                filled.append("pretensao")

            if resume_path:
                try:
                    file_input = page.locator('input[type="file"][name="resume"], input[type="file"]')
                    if file_input.count():
                        file_input.first.set_input_files(resume_path)
                        _pause(0.6, 1.2)
                        filled.append("curriculo")
                except Exception as exc:
                    LOG.warning("InHire resume upload failed: %s", exc)
                    missing.append("curriculo")

            # Hybrid availability / referral — English Yes/No (nth: first Yes = hybrid, second No = not referred).
            _click_visible_choice(page, ("Yes", "Sim"), nth=0)
            _click_visible_choice(page, ("No", "Não", "Nao"), nth=1)

            _fill_diversity_step(page, cfg=cfg)
            filled.append("diversidade")
            return FillResult(ok=not missing, filled=filled, missing=missing)
        except Exception as exc:
            return FillResult(ok=False, filled=filled, missing=missing, error=str(exc))
