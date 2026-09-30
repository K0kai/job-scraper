"""Transporte de IA síncrono compartilhado (Gemini/OpenAI) — 1 prompt → 1 texto.

Refatorado do corpo de apply_channels.generate_open_answer; resume_pipeline tem
o seu próprio caminho de análise (não tocar). levanta AiUnavailableError para
problemas de chave/quota, ValueError para HTTP genérico.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from resume_pipeline import AiUnavailableError


def call_ai_text(
    *,
    prompt: str,
    provider: str,
    model: str,
    api_key: str,
    max_output_tokens: int = 450,
    temperature: float = 0.2,
    timeout: int = 60,
) -> str:
    """Chama o provedor configurado e devolve o texto puro da resposta."""
    if not api_key:
        raise AiUnavailableError("IA indisponivel: chave nao configurada.")
    provider = provider.casefold().strip()
    try:
        if provider == "openai":
            endpoint = "https://api.openai.com/v1/responses"
            payload = json.dumps(
                {"model": model, "input": prompt, "store": False, "max_output_tokens": max_output_tokens}
            ).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == "gemini":
            endpoint = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
            )
            payload = json.dumps(
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_output_tokens, "temperature": temperature},
                }
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            raise ValueError("Provedor de IA invalido.")
        request = Request(endpoint, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code in {401, 403, 429} or "quota" in body.casefold():
            raise AiUnavailableError(f"IA indisponivel (HTTP {exc.code}).") from exc
        raise
    except Exception as exc:
        msg = str(exc).casefold()
        if "quota" in msg or "api key" in msg or "401" in msg or "429" in msg:
            raise AiUnavailableError(str(exc)) from exc
        raise

    if provider == "openai":
        return "\n".join(
            part.get("text", "")
            for item in result.get("output", [])
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ).strip()
    return "\n".join(
        part.get("text", "")
        for item in result.get("candidates", [])
        for part in item.get("content", {}).get("parts", [])
    ).strip()
