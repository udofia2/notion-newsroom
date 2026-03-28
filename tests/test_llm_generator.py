from __future__ import annotations

from types import SimpleNamespace

from newsroom.llm.generator import _use_gemini


def test_use_gemini_when_api_key_present() -> None:
    settings = SimpleNamespace(
        gemini=SimpleNamespace(api_key="abc123"),
    )
    assert _use_gemini(settings) is True


def test_use_ollama_when_gemini_key_missing() -> None:
    settings = SimpleNamespace(
        gemini=SimpleNamespace(api_key=""),
    )
    assert _use_gemini(settings) is False
