from __future__ import annotations

import importlib
import inspect
import sys
import types
from typing import Any

import pytest


class _FakeBoundLogger:
    def bind(self, **kwargs: Any) -> "_FakeBoundLogger":
        del kwargs
        return self

    def info(self, event: str, **kwargs: Any) -> None:
        del event, kwargs

    def exception(self, event: str, **kwargs: Any) -> None:
        del event, kwargs


class _FakeFastMCP:
    def __init__(self, name: str) -> None:
        self.name = name

    def tool(self):
        def _decorator(func):
            return func

        return _decorator


@pytest.fixture
def mcp_module(monkeypatch: pytest.MonkeyPatch):
    structlog_stub = types.SimpleNamespace(get_logger=lambda _name=None: _FakeBoundLogger())
    fastmcp_stub = types.SimpleNamespace(FastMCP=_FakeFastMCP)

    monkeypatch.setitem(sys.modules, "structlog", structlog_stub)
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp_stub)

    module = importlib.import_module("newsroom.mcp.server")
    module = importlib.reload(module)
    return module


def test_mcp_tool_signatures(mcp_module: Any) -> None:
    expected = {
        "search_historical_context": ["page_id", "query", "limit", "filters"],
        "append_historical_block": ["page_id", "query", "limit", "filters"],
        "generate_followup_angles": ["page_id", "query", "top_n"],
        "audit_narrative": ["page_id", "brand_guide_page_id", "post_comment"],
        "prepare_for_publication": ["page_id"],
    }

    for name, params in expected.items():
        fn = getattr(mcp_module, name)
        signature = inspect.signature(fn)
        assert list(signature.parameters.keys()) == params


@pytest.mark.asyncio
async def test_search_historical_context_response_shape(mcp_module: Any) -> None:
    class FakeChroma:
        async def asearch_historical_context(
            self,
            query: str,
            limit: int,
            filters: dict[str, Any] | None,
        ) -> list[dict[str, Any]]:
            del query, limit, filters
            return [
                {
                    "id": "chunk-1",
                    "score": 0.93,
                    "document": "Historical snippet from prior reporting.",
                    "metadata": {
                        "page_id": "old-page-id",
                        "title": "Old coverage title",
                        "url": "https://example.com/old",
                        "date": "2025-12-01T00:00:00+00:00",
                    },
                }
            ]

    class FakeDeps:
        def chroma(self) -> FakeChroma:
            return FakeChroma()

    mcp_module._deps = FakeDeps()

    result = await mcp_module.search_historical_context(page_id="page-1", query="fintech")

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["source_page_id"] == "old-page-id"
    assert result[0]["title"] == "Old coverage title"


@pytest.mark.asyncio
async def test_append_historical_block_response(mcp_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search(
        page_id: str,
        query: str,
        limit: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        del page_id, query, limit, filters
        return [{"source_page_id": "old-1", "title": "Old", "snippet": "x", "score": 0.8}]

    async def fake_append(
        notion: Any,
        page_id: str,
        contexts: list[dict[str, Any]],
        query: str | None,
    ) -> dict[str, Any]:
        del notion
        return {
            "ok": True,
            "page_id": page_id,
            "contexts": len(contexts),
            "query": query,
        }

    class FakeDeps:
        def notion(self) -> object:
            return object()

    monkeypatch.setattr(mcp_module, "search_historical_context", fake_search)
    monkeypatch.setattr(mcp_module, "append_historical_context_toggle_block", fake_append)
    mcp_module._deps = FakeDeps()

    response = await mcp_module.append_historical_block(page_id="page-9", query="market")

    assert response["ok"] is True
    assert response["page_id"] == "page-9"
    assert response["contexts_appended"] == 1
    assert response["query"] == "market"


@pytest.mark.asyncio
async def test_prepare_for_publication_response(mcp_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeNotion:
        async def list_block_children(self, page_id: str) -> list[dict[str, Any]]:
            del page_id
            return [
                {
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": "First paragraph."}]},
                }
            ]

    class FakeDeps:
        def notion(self) -> FakeNotion:
            return FakeNotion()

    async def fake_cleanup(notion: Any, page_id: str) -> dict[str, int]:
        del notion, page_id
        return {"removed_empty_paragraphs": 1, "demoted_h1_blocks": 0}

    def fake_md(text: str) -> str:
        return f"md::{text}"

    monkeypatch.setattr(mcp_module, "clean_page_formatting_before_publishing", fake_cleanup)
    monkeypatch.setattr(mcp_module, "clean_markdown_for_publishing", fake_md)
    mcp_module._deps = FakeDeps()

    response = await mcp_module.prepare_for_publication(page_id="page-77")

    assert response["ok"] is True
    assert response["page_id"] == "page-77"
    assert response["cleanup"]["removed_empty_paragraphs"] == 1
    assert response["markdown_preview"].startswith("md::")
