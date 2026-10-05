"""Formatação de datas/horas para exibição no painel (Brasília, dd/mm/yyyy)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BRASILIA = ZoneInfo("America/Sao_Paulo")
_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_BR_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def parse_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _calendar_from_iso_prefix(raw: str) -> str | None:
    """YYYY-MM-DD no início da string → dd/mm/yyyy (sem mudar o dia por fuso)."""
    m = _ISO_DATE_PREFIX_RE.match(raw.strip())
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{int(d):02d}/{int(mo):02d}/{y}"


def format_brasilia_date(value: object) -> str:
    """Data dd/mm/yyyy. ISO com hora usa só a parte do calendário (sem deslocar dia)."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if _BR_DATE_RE.match(raw):
        parts = raw.split("/")
        return f"{int(parts[0]):02d}/{int(parts[1]):02d}/{parts[2]}"
    cal = _calendar_from_iso_prefix(raw)
    if cal:
        return cal
    parsed = parse_utc(raw)
    if parsed is None:
        return raw[:10] if len(raw) >= 10 else raw
    return parsed.astimezone(BRASILIA).strftime("%d/%m/%Y")


def format_brasilia(value: object) -> str:
    """Data e hora dd/mm/yyyy HH:MM em Brasília."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "T" not in raw and " " not in raw and _ISO_DATE_PREFIX_RE.match(raw):
        return format_brasilia_date(raw)
    parsed = parse_utc(raw)
    if parsed is None:
        cal = _calendar_from_iso_prefix(raw)
        return cal or raw
    return parsed.astimezone(BRASILIA).strftime("%d/%m/%Y %H:%M")


def format_month_label(value: object) -> str:
    """Rótulo de mês: YYYY-MM → mm/yyyy."""
    raw = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}$", raw):
        y, mo = raw.split("-", 1)
        return f"{mo}/{y}"
    return raw


def format_cycle_range(start: object, end: object) -> str:
    """Intervalo de ciclo Apify (datas legíveis)."""
    s = format_brasilia_date(start)
    e = format_brasilia_date(end)
    if s and e:
        return f"{s} a {e}"
    if s or e:
        return s or e
    return "ciclo atual"


def format_cycle_label_display(label: object) -> str:
    """Reformata rótulos de ciclo já salvos (YYYY-MM-DD a YYYY-MM-DD)."""
    raw = str(label or "").strip()
    if not raw or raw == "—" or raw == "ciclo atual":
        return raw or "—"
    if " a " in raw:
        left, right = raw.split(" a ", 1)
        return format_cycle_range(left.strip(), right.strip())
    return format_brasilia_date(raw)
