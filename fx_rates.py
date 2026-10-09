"""Taxas de câmbio via API gratuita (open.er-api) com cache 12h.

Frankfurter/BCE não publica COP (caso Colômbia); open.er-api cobre COP e demais.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from typing import Callable

LOG = logging.getLogger("odradek-scraper")

FX_JSON_KEY = "fx_rates_json"
FX_AT_KEY = "fx_rates_fetched_at"
TTL_SECONDS = 12 * 3600
API_URL = "https://open.er-api.com/v6/latest/USD"

_memory: dict[str, float] = {}
_memory_at: float = 0.0

PersistFn = Callable[[str, str], None]


def _parse_at(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _rates_from_cfg(cfg: dict | None) -> tuple[dict[str, float], float] | None:
    cfg = cfg or {}
    raw = (cfg.get(FX_JSON_KEY) or "").strip()
    if not raw:
        return None
    at = _parse_at(str(cfg.get(FX_AT_KEY) or "")) or 0.0
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    rates: dict[str, float] = {}
    for key, value in data.items():
        try:
            rates[str(key).upper()] = float(value)
        except (TypeError, ValueError):
            continue
    if not rates:
        return None
    return rates, at


def _fetch_usd_rates(url: str = API_URL, *, timeout: float = 12) -> dict[str, float]:
    req = urllib.request.Request(url, headers={"User-Agent": "odradek-scraper-fx/1.0"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rates_raw = payload.get("rates") if isinstance(payload, dict) else None
    if not isinstance(rates_raw, dict):
        raise ValueError("resposta FX sem rates")
    out: dict[str, float] = {"USD": 1.0}
    for key, value in rates_raw.items():
        try:
            out[str(key).upper()] = float(value)
        except (TypeError, ValueError):
            continue
    if "USD" not in out:
        out["USD"] = 1.0
    return out


def refresh_rates(
    cfg: dict | None = None,
    *,
    persist: PersistFn | None = None,
    fetch_fn=_fetch_usd_rates,
    now: float | None = None,
) -> dict[str, float]:
    """Busca taxas (base USD) e atualiza memória + cfg/settings opcional."""
    global _memory, _memory_at
    rates = fetch_fn()
    stamp = now if now is not None else time.time()
    _memory = dict(rates)
    _memory_at = stamp
    iso = datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(rates, ensure_ascii=False, sort_keys=True)
    if cfg is not None:
        cfg[FX_JSON_KEY] = payload
        cfg[FX_AT_KEY] = iso
    if persist is not None:
        try:
            persist(FX_JSON_KEY, payload)
            persist(FX_AT_KEY, iso)
        except Exception as exc:
            LOG.debug("fx persist falhou: %s", exc)
    else:
        try:
            from app import set_setting

            set_setting(FX_JSON_KEY, payload)
            set_setting(FX_AT_KEY, iso)
        except Exception as exc:
            LOG.debug("fx set_setting falhou: %s", exc)
    return rates


def get_usd_rates(
    cfg: dict | None = None,
    *,
    persist: PersistFn | None = None,
    fetch_fn=_fetch_usd_rates,
    ttl: float = TTL_SECONDS,
    now: float | None = None,
) -> dict[str, float]:
    """Taxas vs USD: memória → settings → API."""
    global _memory, _memory_at
    stamp = now if now is not None else time.time()
    if _memory and (stamp - _memory_at) < ttl:
        return dict(_memory)
    cached = _rates_from_cfg(cfg)
    if cached:
        rates, at = cached
        if at and (stamp - at) < ttl:
            _memory = dict(rates)
            _memory_at = at
            return dict(rates)
    try:
        return refresh_rates(cfg, persist=persist, fetch_fn=fetch_fn, now=stamp)
    except Exception as exc:
        LOG.warning("fx fetch falhou: %s", exc)
        if _memory:
            return dict(_memory)
        if cached:
            return dict(cached[0])
        return {}


def convert_amount(
    amount: float,
    from_ccy: str,
    to_ccy: str,
    *,
    cfg: dict | None = None,
    rates: dict[str, float] | None = None,
) -> float | None:
    """Converte `amount` de from_ccy → to_ccy via taxas base USD."""
    if amount is None or amount <= 0:
        return None
    src = (from_ccy or "").upper().strip()
    dst = (to_ccy or "").upper().strip()
    if not src or not dst:
        return None
    if src == dst:
        return float(amount)
    table = rates if rates is not None else get_usd_rates(cfg)
    if not table:
        return None
    if src != "USD" and src not in table:
        return None
    if dst != "USD" and dst not in table:
        return None
    in_usd = float(amount) if src == "USD" else float(amount) / float(table[src])
    if dst == "USD":
        return in_usd
    return in_usd * float(table[dst])


def clear_memory_cache() -> None:
    global _memory, _memory_at
    _memory = {}
    _memory_at = 0.0


__all__ = [
    "API_URL",
    "FX_AT_KEY",
    "FX_JSON_KEY",
    "TTL_SECONDS",
    "clear_memory_cache",
    "convert_amount",
    "get_usd_rates",
    "refresh_rates",
]
