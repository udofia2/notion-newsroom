from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

context_hunter = importlib.import_module("newsroom.workflows.context_hunter")


def test_query_quality_validation_rejects_generic_fallback() -> None:
    assert context_hunter._is_valid_generated_query("Test: Context Hunter background context analysis") is False


def test_query_quality_validation_accepts_specific_query() -> None:
    assert context_hunter._is_valid_generated_query(
        "AI journalism adoption statistics BBC New York Times automation verification claims"
    ) is True


class FakeNotionClient:
    def __init__(self, blocks: list[dict[str, Any]]) -> None:
        self._blocks = blocks
        self.created_comments: list[dict[str, Any]] = []

    async def get_page_model(self, page_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=page_id, title="Fintech liquidity pressure in Q1")

    async def list_block_children(self, page_id: str) -> list[dict[str, Any]]:
        return self._blocks

    async def create_comment(self, page_id: str, rich_text: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"page_id": page_id, "rich_text": rich_text}
        self.created_comments.append(payload)
        return {"id": "comment-1", **payload}


class FakeChromaManager:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results

    def search_historical_context(
        self,
        query: str,
        limit: int,
        filters: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        del query, limit, filters
        return self._results


@pytest.fixture
def fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        chroma=SimpleNamespace(top_k=8, relevance_threshold=0.55),
        ollama=SimpleNamespace(host="http://localhost:11434", generation_model="llama3.2:3b"),
        request_timeout_seconds=30,
    )


@pytest.mark.asyncio
async def test_run_context_hunter_appends_context_block(
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: SimpleNamespace,
) -> None:
    blocks = [
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "plain_text": "The market is shifting toward profitability and tighter runway discipline."
                    }
                ]
            },
        }
    ]
    search_results = [
        {
            "id": "chunk-1",
            "score": 0.88,
            "document": "Earlier reporting shows founders prioritized cashflow after Q4 contraction.",
            "metadata": {
                "page_id": "old-page-1",
                "title": "How startups survived Q4",
                "url": "https://example.com/old-page-1",
                "date": "2026-01-01T00:00:00+00:00",
            },
        }
    ]

    fake_notion = FakeNotionClient(blocks=blocks)
    fake_chroma = FakeChromaManager(results=search_results)

    appended: dict[str, Any] = {}

    async def fake_append(
        notion: Any,
        page_id: str,
        contexts: list[dict[str, Any]],
        query: str | None,
    ) -> dict[str, Any]:
        appended["notion"] = notion
        appended["page_id"] = page_id
        appended["contexts"] = contexts
        appended["query"] = query
        return {"ok": True, "block_id": "toggle-1"}

    async def fake_query(title: str, content: str, settings: Any) -> str:
        del title, content, settings
        return "fintech liquidity cashflow runway q1"

    monkeypatch.setattr(context_hunter, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(context_hunter, "_get_notion_client", lambda _settings: fake_notion)
    monkeypatch.setattr(context_hunter, "_get_chroma_manager", lambda _settings: fake_chroma)
    monkeypatch.setattr(context_hunter, "_generate_search_query", fake_query)
    monkeypatch.setattr(context_hunter, "append_historical_context_toggle_block", fake_append)

    result = await context_hunter.run_context_hunter("page-123")

    assert result["ok"] is True
    assert result["status"] == "context_appended"
    assert result["contexts_appended"] == 1
    assert appended["page_id"] == "page-123"
    assert appended["query"] == "fintech liquidity cashflow runway q1"
    assert appended["contexts"][0]["source_page_id"] == "old-page-1"


@pytest.mark.asyncio
async def test_run_context_hunter_returns_no_relevant_context(
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: SimpleNamespace,
) -> None:
    blocks = [
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"plain_text": "Short update on macro trend."}]
            },
        }
    ]
    weak_results = [
        {
            "id": "chunk-weak",
            "score": 0.10,
            "document": "Weakly related content.",
            "metadata": {"page_id": "old-2", "title": "Old title"},
        }
    ]

    fake_notion = FakeNotionClient(blocks=blocks)
    fake_chroma = FakeChromaManager(results=weak_results)

    async def fake_query(title: str, content: str, settings: Any) -> str:
        del title, content, settings
        return "macro trend impact"

    monkeypatch.setattr(context_hunter, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(context_hunter, "_get_notion_client", lambda _settings: fake_notion)
    monkeypatch.setattr(context_hunter, "_get_chroma_manager", lambda _settings: fake_chroma)
    monkeypatch.setattr(context_hunter, "_generate_search_query", fake_query)

    result = await context_hunter.run_context_hunter("page-456")

    assert result["ok"] is True
    assert result["status"] == "no_relevant_context"
    assert result["contexts_appended"] == 0
    assert len(fake_notion.created_comments) == 1
    assert "no relevant historical context was found" in fake_notion.created_comments[0]["rich_text"][0]["text"]["content"].lower()


@pytest.mark.asyncio
async def test_run_context_hunter_recovers_when_embedding_model_missing(
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: SimpleNamespace,
) -> None:
    blocks = [
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"plain_text": "A draft needing background context."}]
            },
        }
    ]

    class FailingChromaManager:
        def search_historical_context(
            self,
            query: str,
            limit: int,
            filters: dict[str, Any] | None,
        ) -> list[dict[str, Any]]:
            del query, limit, filters
            raise RuntimeError('model "nomic-embed-text:v1.5" not found, try pulling it first (status code: 404)')

    async def fake_query(title: str, content: str, settings: Any) -> str:
        del title, content, settings
        return "background context draft"

    fake_notion = FakeNotionClient(blocks=blocks)

    monkeypatch.setattr(context_hunter, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(context_hunter, "_get_notion_client", lambda _settings: fake_notion)
    monkeypatch.setattr(context_hunter, "_get_chroma_manager", lambda _settings: FailingChromaManager())
    monkeypatch.setattr(context_hunter, "_generate_search_query", fake_query)

    result = await context_hunter.run_context_hunter("page-789")

    assert result["ok"] is True
    assert result["status"] == "search_unavailable"
    assert result["contexts_appended"] == 0
    assert len(fake_notion.created_comments) == 1
    assert "could not search the historical archive" in fake_notion.created_comments[0]["rich_text"][0]["text"]["content"].lower()


@pytest.mark.asyncio
async def test_run_context_hunter_marks_unreliable_when_query_generation_fails(
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: SimpleNamespace,
) -> None:
    blocks = [
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"plain_text": "AI is changing newsroom workflows and verification patterns."}]
            },
        }
    ]

    fake_notion = FakeNotionClient(blocks=blocks)

    async def fail_query(title: str, content: str, settings: Any) -> str:
        del title, content, settings
        raise RuntimeError("generation timeout")

    class NoopChroma:
        def search_historical_context(
            self,
            query: str,
            limit: int,
            filters: dict[str, Any] | None,
        ) -> list[dict[str, Any]]:
            del query, limit, filters
            return []

    monkeypatch.setattr(context_hunter, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(context_hunter, "_get_notion_client", lambda _settings: fake_notion)
    monkeypatch.setattr(context_hunter, "_get_chroma_manager", lambda _settings: NoopChroma())
    monkeypatch.setattr(context_hunter, "_generate_search_query", fail_query)

    result = await context_hunter.run_context_hunter("page-query-fail")

    assert result["ok"] is True
    assert result["status"] == "query_generation_failed"
    assert result["reliable"] is False
    assert result["contexts_appended"] == 0
    assert len(fake_notion.created_comments) == 1
    assert "could not generate a reliable search query" in fake_notion.created_comments[0]["rich_text"][0]["text"]["content"].lower()
    assert "reason:" in fake_notion.created_comments[0]["rich_text"][0]["text"]["content"].lower()


@pytest.mark.asyncio
async def test_run_context_hunter_accepts_short_archive_source_id(
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: SimpleNamespace,
) -> None:
    blocks = [
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"plain_text": "Flood resilience mechanisms and emergency response funding."}]
            },
        }
    ]
    search_results = [
        {
            "id": "chunk-1",
            "score": 0.91,
            "document": "Lagos pilots parametric insurance and rapid disbursement triggers.",
            "metadata": {
                "page_id": "191080",
                "title": "Flood insurance pilot",
                "url": "https://example.com/flood-insurance-pilot",
                "date": "2026-03-20T00:00:00+00:00",
            },
        }
    ]

    fake_notion = FakeNotionClient(blocks=blocks)
    fake_chroma = FakeChromaManager(results=search_results)

    async def fake_append(
        notion: Any,
        page_id: str,
        contexts: list[dict[str, Any]],
        query: str | None,
    ) -> dict[str, Any]:
        del notion, page_id, query
        assert contexts[0]["source_page_id"] == "191080"
        return {"ok": True, "block_id": "toggle-short-id"}

    async def fake_query(title: str, content: str, settings: Any) -> str:
        del title, content, settings
        return "Lagos parametric flood insurance emergency response financing"

    monkeypatch.setattr(context_hunter, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(context_hunter, "_get_notion_client", lambda _settings: fake_notion)
    monkeypatch.setattr(context_hunter, "_get_chroma_manager", lambda _settings: fake_chroma)
    monkeypatch.setattr(context_hunter, "_generate_search_query", fake_query)
    monkeypatch.setattr(context_hunter, "append_historical_context_toggle_block", fake_append)

    result = await context_hunter.run_context_hunter("page-short-id")

    assert result["ok"] is True
    assert result["status"] == "context_appended"
    assert result["contexts_appended"] == 1


@pytest.mark.asyncio
async def test_run_context_hunter_returns_no_content_for_empty_page(
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: SimpleNamespace,
) -> None:
    blocks = [
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"plain_text": "   "}]
            },
        }
    ]

    fake_notion = FakeNotionClient(blocks=blocks)

    async def fail_query(title: str, content: str, settings: Any) -> str:
        del title, content, settings
        raise AssertionError("query generation should not run for empty content")

    monkeypatch.setattr(context_hunter, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(context_hunter, "_get_notion_client", lambda _settings: fake_notion)
    monkeypatch.setattr(context_hunter, "_generate_search_query", fail_query)

    result = await context_hunter.run_context_hunter("page-empty")

    assert result["ok"] is True
    assert result["status"] == "no_content"
    assert result["contexts_appended"] == 0
    assert len(fake_notion.created_comments) == 1
    assert "draft content is empty" in fake_notion.created_comments[0]["rich_text"][0]["text"]["content"]


@pytest.mark.asyncio
async def test_run_context_hunter_ignores_artifact_only_content(
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: SimpleNamespace,
) -> None:
    blocks = [
        {
            "type": "toggle",
            "toggle": {
                "rich_text": [{"plain_text": "Historical Context for: fintech trends"}],
            },
        },
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"plain_text": "No relevant historical context was found."}],
            },
        },
        {
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{"plain_text": "Related: old-page-id | score=0.80 | Older story"}],
            },
        },
    ]

    fake_notion = FakeNotionClient(blocks=blocks)

    class NoopChroma:
        def search_historical_context(
            self,
            query: str,
            limit: int,
            filters: dict[str, Any] | None,
        ) -> list[dict[str, Any]]:
            del query, limit, filters
            raise AssertionError("vector search should not run for artifact-only content")

    async def fail_query(title: str, content: str, settings: Any) -> str:
        del title, content, settings
        raise AssertionError("query generation should not run for artifact-only content")

    monkeypatch.setattr(context_hunter, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(context_hunter, "_get_notion_client", lambda _settings: fake_notion)
    monkeypatch.setattr(context_hunter, "_get_chroma_manager", lambda _settings: NoopChroma())
    monkeypatch.setattr(context_hunter, "_generate_search_query", fail_query)

    result = await context_hunter.run_context_hunter("page-artifacts")

    assert result["ok"] is True
    assert result["status"] == "no_content"
    assert result["contexts_appended"] == 0
    assert len(fake_notion.created_comments) == 1
    assert "draft content is empty" in fake_notion.created_comments[0]["rich_text"][0]["text"]["content"]
