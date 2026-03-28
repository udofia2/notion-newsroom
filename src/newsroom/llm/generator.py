"""Provider-aware text generation with Gemini-first routing and Ollama fallback."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from newsroom.config import Settings

logger = logging.getLogger(__name__)


def _use_gemini(settings: Settings) -> bool:
    gemini = getattr(settings, "gemini", None)
    api_key = getattr(gemini, "api_key", None) if gemini is not None else None
    return isinstance(api_key, str) and bool(api_key.strip())


async def _generate_with_ollama(
    settings: Settings,
    prompt: str,
    *,
    system: str | None,
    timeout_seconds: int,
) -> str:
    payload: dict[str, Any] = {
        "model": settings.ollama.generation_model,
        "prompt": prompt,
        "stream": True,
    }
    if system and system.strip():
        payload["system"] = system

    chunks: list[str] = []
    async with httpx.AsyncClient(
        base_url=str(settings.ollama.host),
        timeout=timeout_seconds,
    ) as client:
        async with client.stream("POST", "/api/generate", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                piece = data.get("response")
                if isinstance(piece, str) and piece:
                    chunks.append(piece)
                if data.get("done"):
                    break

    text = "".join(chunks).strip()
    if not text:
        raise ValueError("Ollama returned empty streamed response")
    return text


async def _generate_with_gemini(
    settings: Settings,
    prompt: str,
    *,
    system: str | None,
    timeout_seconds: int,
) -> str:
    gemini = settings.gemini
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    if system and system.strip():
        payload["system_instruction"] = {"parts": [{"text": system}]}

    endpoint = f"/v1beta/models/{gemini.model}:generateContent"
    async with httpx.AsyncClient(
        base_url=str(gemini.base_url),
        timeout=timeout_seconds,
    ) as client:
        response = await client.post(endpoint, params={"key": gemini.api_key}, json=payload)
        response.raise_for_status()
        body = response.json()

    candidates = body.get("candidates", []) if isinstance(body, dict) else []
    texts: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content", {})
        if not isinstance(content, dict):
            continue
        parts = content.get("parts", [])
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)

    output = "\n".join(texts).strip()
    if not output:
        raise ValueError("Gemini returned empty content")
    return output


async def generate_text(
    settings: Settings,
    prompt: str,
    *,
    system: str | None = None,
    timeout_seconds: int | None = None,
) -> str:
    """Generate text using Gemini when configured; otherwise fallback to Ollama."""

    resolved_timeout = timeout_seconds if timeout_seconds is not None else settings.request_timeout_seconds
    started_at = time.monotonic()

    if _use_gemini(settings):
        provider = "gemini"
        text = await _generate_with_gemini(
            settings=settings,
            prompt=prompt,
            system=system,
            timeout_seconds=resolved_timeout,
        )
    else:
        provider = "ollama"
        text = await _generate_with_ollama(
            settings=settings,
            prompt=prompt,
            system=system,
            timeout_seconds=resolved_timeout,
        )

    duration_seconds = round(time.monotonic() - started_at, 3)
    logger.info("llm_generation_completed provider=%s duration_seconds=%s", provider, duration_seconds)
    return text
