"""Rastreamento de cotas: Apify (API oficial) + estimativa local de IA.

A parte de IA é deliberadamente local (contadores deste app). Não representa a
cota free oficial do provedor — o painel deve deixar isso explícito.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable

STORE_KEY = "ai_usage_json"
APIFY_SNAPSHOT_KEY = "apify_quota_snapshot_json"
_LOCK = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(provider: str, when: datetime) -> str:
    return f"{provider.casefold()}|{when.astimezone(timezone.utc).strftime('%Y-%m-%d')}"


def _month_key(provider: str, when: datetime) -> str:
    return f"{provider.casefold()}|{when.astimezone(timezone.utc).strftime('%Y-%m')}"


def _empty_bucket() -> dict[str, Any]:
    return {
        "calls_ok": 0,
        "calls_error": 0,
        "calls_quota_error": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "last_model": "",
    }


def load_store(raw: str | None) -> dict[str, Any]:
    if not raw or not str(raw).strip():
        return {"days": {}, "months": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"days": {}, "months": {}}
    if not isinstance(data, dict):
        return {"days": {}, "months": {}}
    data.setdefault("days", {})
    data.setdefault("months", {})
    if not isinstance(data["days"], dict):
        data["days"] = {}
    if not isinstance(data["months"], dict):
        data["months"] = {}
    return data


def dump_store(store: dict[str, Any]) -> str:
    return json.dumps(store, ensure_ascii=False, separators=(",", ":"))


def extract_tokens(provider: str, result: dict[str, Any] | None) -> tuple[int, int]:
    """Extrai (tokens_in, tokens_out) do payload do provedor, se existir."""
    if not isinstance(result, dict):
        return 0, 0
    provider = provider.casefold().strip()
    if provider == "gemini":
        meta = result.get("usageMetadata") or {}
        tin = int(meta.get("promptTokenCount") or 0)
        tout = int(meta.get("candidatesTokenCount") or meta.get("totalTokenCount") or 0)
        if tout and meta.get("candidatesTokenCount") is None and meta.get("promptTokenCount"):
            # totalTokenCount às vezes inclui prompt — preferir só candidates se houver
            try:
                total = int(meta.get("totalTokenCount") or 0)
                if total >= tin:
                    tout = total - tin
            except (TypeError, ValueError):
                pass
        return max(0, tin), max(0, tout)
    if provider == "openai":
        usage = result.get("usage") or {}
        tin = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        tout = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        return max(0, tin), max(0, tout)
    return 0, 0


def _bump_bucket(bucket: dict[str, Any], *, tokens_in: int, tokens_out: int, ok: bool, quota_error: bool, model: str) -> None:
    if ok:
        bucket["calls_ok"] = int(bucket.get("calls_ok") or 0) + 1
    else:
        bucket["calls_error"] = int(bucket.get("calls_error") or 0) + 1
    if quota_error:
        bucket["calls_quota_error"] = int(bucket.get("calls_quota_error") or 0) + 1
    bucket["tokens_in"] = int(bucket.get("tokens_in") or 0) + max(0, int(tokens_in or 0))
    bucket["tokens_out"] = int(bucket.get("tokens_out") or 0) + max(0, int(tokens_out or 0))
    if model:
        bucket["last_model"] = str(model)


def record_ai_call(
    store: dict[str, Any],
    *,
    provider: str,
    model: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    ok: bool = True,
    quota_error: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Incrementa contadores locais do dia e do mês UTC para o provedor."""
    provider = (provider or "unknown").casefold().strip() or "unknown"
    when = now or _utc_now()
    store.setdefault("days", {})
    store.setdefault("months", {})
    dk = _day_key(provider, when)
    mk = _month_key(provider, when)
    day = store["days"].get(dk) or _empty_bucket()
    month = store["months"].get(mk) or _empty_bucket()
    _bump_bucket(day, tokens_in=tokens_in, tokens_out=tokens_out, ok=ok, quota_error=quota_error, model=model)
    _bump_bucket(month, tokens_in=tokens_in, tokens_out=tokens_out, ok=ok, quota_error=quota_error, model=model)
    store["days"][dk] = day
    store["months"][mk] = month
    _prune_old(store, when)
    return store


def _prune_old(store: dict[str, Any], when: datetime, *, keep_days: int = 45, keep_months: int = 6) -> None:
    """Remove chaves antigas para o JSON de settings não crescer sem limite."""
    # Simplificado: mantém no máx. keep_days entradas de dia e keep_months de mês.
    days = store.get("days") or {}
    if len(days) > keep_days:
        for key in sorted(days.keys())[: len(days) - keep_days]:
            days.pop(key, None)
    months = store.get("months") or {}
    if len(months) > keep_months:
        for key in sorted(months.keys())[: len(months) - keep_months]:
            months.pop(key, None)


def summarize_ai_usage(
    store: dict[str, Any],
    *,
    provider: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    provider = (provider or "").casefold().strip() or "unknown"
    when = now or _utc_now()
    day = dict((store.get("days") or {}).get(_day_key(provider, when)) or _empty_bucket())
    month = dict((store.get("months") or {}).get(_month_key(provider, when)) or _empty_bucket())
    return {
        "provider": provider,
        "day": day,
        "month": month,
        "day_label": when.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "month_label": when.astimezone(timezone.utc).strftime("%Y-%m"),
        "is_local_estimate": True,
    }


def parse_apify_limits(payload: dict[str, Any], *, local_limit_usd: float) -> dict[str, Any]:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    limits = data.get("limits") or {}
    current = data.get("current") or {}
    cycle = data.get("monthlyUsageCycle") or data.get("usageCycle") or {}
    used = float(current.get("monthlyUsageUsd") or 0)
    api_limit = float(limits.get("maxMonthlyUsageUsd") or 0)
    start = str(cycle.get("startAt", ""))[:10]
    end = str(cycle.get("endAt", ""))[:10]
    label = f"{start} a {end}" if start or end else "ciclo atual"
    local = max(0.0, float(local_limit_usd or 0))
    pct_local = (used / local * 100.0) if local > 0 else 0.0
    pct_api = (used / api_limit * 100.0) if api_limit > 0 else 0.0
    return {
        "used_usd": used,
        "api_limit_usd": api_limit,
        "local_limit_usd": local,
        "cycle_label": label,
        "pct_of_local": min(999.0, pct_local),
        "pct_of_api": min(999.0, pct_api),
        "fetched_at": _utc_now().isoformat(timespec="seconds"),
        "error": "",
    }


def persist_ai_call(
    *,
    get_raw: Callable[[], str],
    set_raw: Callable[[str], None],
    provider: str,
    model: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    ok: bool = True,
    quota_error: bool = False,
) -> None:
    """Persiste um incremento (thread-safe) via callbacks de settings."""
    with _LOCK:
        store = load_store(get_raw())
        record_ai_call(
            store,
            provider=provider,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            ok=ok,
            quota_error=quota_error,
        )
        set_raw(dump_store(store))


def record_from_ai_result(
    *,
    get_raw: Callable[[], str],
    set_raw: Callable[[str], None],
    provider: str,
    model: str,
    result: dict[str, Any] | None,
    ok: bool,
    quota_error: bool = False,
) -> None:
    tin, tout = extract_tokens(provider, result)
    persist_ai_call(
        get_raw=get_raw,
        set_raw=set_raw,
        provider=provider,
        model=model,
        tokens_in=tin,
        tokens_out=tout,
        ok=ok,
        quota_error=quota_error,
    )


_PERSIST: tuple[Callable[[], str], Callable[[str], None]] | None = None


def configure_persistence(get_raw: Callable[[], str], set_raw: Callable[[str], None]) -> None:
    """Liga o tracker ao store de settings do app (evita import circular)."""
    global _PERSIST
    _PERSIST = (get_raw, set_raw)


def try_record_ai_result(
    *,
    provider: str,
    model: str,
    result: dict[str, Any] | None = None,
    ok: bool = True,
    quota_error: bool = False,
) -> None:
    """No-op se a persistência ainda não foi configurada."""
    if _PERSIST is None:
        return
    try:
        get_raw, set_raw = _PERSIST
        record_from_ai_result(
            get_raw=get_raw,
            set_raw=set_raw,
            provider=provider,
            model=model,
            result=result,
            ok=ok,
            quota_error=quota_error,
        )
    except Exception:
        # Nunca derruba a chamada de IA por falha de métrica.
        pass
