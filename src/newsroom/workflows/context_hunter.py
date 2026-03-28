"""Context Hunter workflow.

Finds historical context for a Notion page and appends a linked toggle block.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from newsroom.chroma.manager import ChromaManager
from newsroom.config import Settings, get_settings
from newsroom.llm import generate_text
from newsroom.notion.blocks import append_historical_context_toggle_block
from newsroom.notion.client import NotionClient
from newsroom.types import HistoricalContext

logger = logging.getLogger(__name__)

_CONTEXT_ARTIFACT_PREFIXES = (
    "Historical Context",
    "Related:",
)
_CONTEXT_ARTIFACT_EXACT_LINES = {
    "No relevant historical context was found.",
}


class ContextSearchUnavailableError(RuntimeError):
    """Raised when vector search infrastructure is temporarily unavailable."""


def _is_context_artifact_line(text: str) -> bool:
    normalized = " ".join(text.split()).strip()
    if not normalized:
        return True
    if normalized in _CONTEXT_ARTIFACT_EXACT_LINES:
        return True
    return any(normalized.startswith(prefix) for prefix in _CONTEXT_ARTIFACT_PREFIXES)


def _extract_plain_text(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        payload = block.get(block_type) if isinstance(block_type, str) else None
        rich_text = payload.get("rich_text", []) if isinstance(payload, dict) else []
        text = "".join(part.get("plain_text", "") for part in rich_text if isinstance(part, dict)).strip()
        if text:
            lines.append(text)
    return "\n\n".join(lines).strip()


def _strip_context_artifacts(text: str) -> str:
    if not text.strip():
        return ""
    cleaned_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not _is_context_artifact_line(line)
    ]
    return "\n".join(cleaned_lines).strip()


async def _post_no_content_comment(notion: NotionClient, page_id: str) -> dict[str, Any]:
    message = (
        "Context Hunter skipped this run because the draft content is empty. "
        "Add body text, then rerun with status set to Researching."
    )
    return await notion.create_comment(
        page_id=page_id,
        rich_text=[
            {
                "type": "text",
                "text": {
                    "content": message,
                },
            }
        ],
    )


def _build_query_prompt(title: str, content: str) -> str:
    snippet = content[:3500]
    return (
        "You generate semantic retrieval queries for journalism archives.\n"
        "Return exactly one concise search query (8-18 words), no quotes, no markdown.\n"
        "Prioritize entities, themes, geography, and timeframe from the draft.\n\n"
        f"Title: {title}\n"
        f"Draft excerpt:\n{snippet}\n"
    )


def _is_valid_generated_query(query: str) -> bool:
    normalized = " ".join(query.split()).strip()
    if not normalized:
        return False

    words = normalized.split(" ")
    if len(words) < 5 or len(words) > 24:
        return False

    disallowed_phrases = (
        "background context analysis",
        "test: context hunter",
    )
    lowered = normalized.lower()
    return not any(phrase in lowered for phrase in disallowed_phrases)


async def _generate_search_query(
    title: str,
    content: str,
    settings: Settings,
) -> str:
    prompt = _build_query_prompt(title=title, content=content)
    timeout_seconds = min(60, settings.request_timeout_seconds)
    try:
        raw = await generate_text(settings=settings, prompt=prompt, timeout_seconds=timeout_seconds)
    except httpx.ReadTimeout:
        logger.warning("context_query_generation_timeout timeout_seconds=%s", timeout_seconds)
        raise
    if not raw:
        raise ValueError("Model returned empty response")

    query = " ".join(raw.split())[:220]
    if not _is_valid_generated_query(query):
        raise ValueError("Generated query failed quality checks")
    return query


def _describe_query_generation_failure(exc: Exception) -> str:
    if isinstance(exc, httpx.ReadTimeout):
        return "query generation timed out due to a temporary network delay"
    if isinstance(exc, httpx.ConnectError):
        return "query generation failed due to a network connectivity issue"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 429:
            return "query generation was rate-limited by the provider"
        if status_code >= 500:
            return f"query generation failed because the provider returned server error {status_code}"
        return f"query generation failed because the provider returned HTTP {status_code}"
    return "query generation failed unexpectedly"


async def _post_query_failure_comment(notion: NotionClient, page_id: str, reason: str) -> dict[str, Any]:
    message = (
        "Context Hunter could not generate a reliable search query for this draft. "
        f"Reason: {reason}. No historical context was added in this run."
    )
    return await notion.create_comment(
        page_id=page_id,
        rich_text=[
            {
                "type": "text",
                "text": {
                    "content": message,
                },
            }
        ],
    )


async def _post_search_unavailable_comment(notion: NotionClient, page_id: str, reason: str) -> dict[str, Any]:
    message = (
        "Context Hunter could not search the historical archive in this run. "
        f"Reason: {reason}. Please retry shortly."
    )
    return await notion.create_comment(
        page_id=page_id,
        rich_text=[
            {
                "type": "text",
                "text": {
                    "content": message,
                },
            }
        ],
    )


async def _post_no_relevant_context_comment(notion: NotionClient, page_id: str, query: str) -> dict[str, Any]:
    message = (
        "No relevant historical context was found. "
        f"Context Hunter searched with query: '{query}'."
    )
    return await notion.create_comment(
        page_id=page_id,
        rich_text=[
            {
                "type": "text",
                "text": {
                    "content": message,
                },
            }
        ],
    )


def _to_historical_context(match: dict[str, Any], relevance_threshold: float) -> HistoricalContext | None:
    metadata = match.get("metadata", {}) if isinstance(match, dict) else {}
    snippet = str(match.get("document") or "").strip()
    if not snippet:
        return None
    score = float(match.get("score") or 0.0)
    if score < relevance_threshold:
        return None

    published_at = metadata.get("date")
    parsed_date: datetime | None = None
    if isinstance(published_at, str) and published_at.strip():
        try:
            parsed_date = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            parsed_date = None

    return HistoricalContext(
        source_page_id=str(metadata.get("page_id") or "unknown"),
        title=str(metadata.get("title") or "Untitled"),
        snippet=snippet,
        score=min(1.0, max(0.0, score)),
        url=metadata.get("url") or None,
        published_at=parsed_date,
    )


def _get_notion_client(settings: Settings) -> NotionClient:
    return NotionClient(settings=settings)


def _get_chroma_manager(settings: Settings) -> ChromaManager:
    embedding_model = getattr(settings.ollama, "embedding_model", "nomic-embed-text:v1.5")
    collection_name = getattr(settings.chroma, "collection_name", "notion_newsroom_archive")
    return ChromaManager(
        persist_directory=settings.chroma.persist_directory,
        ollama_host=str(settings.ollama.host),
        embedding_model=embedding_model,
        collection_name=collection_name,
    )


async def _fetch_page_snapshot(notion: NotionClient, page_id: str) -> tuple[str, str]:
    page_task = notion.get_page_model(page_id)
    blocks_task = notion.list_block_children(page_id)
    page, blocks = await asyncio.gather(page_task, blocks_task)
    return page.title, _extract_plain_text(blocks)


async def _search_relevant_contexts(
    chroma: ChromaManager,
    query: str,
    limit: int,
    relevance_threshold: float,
) -> list[HistoricalContext]:
    try:
        raw_results = await asyncio.to_thread(chroma.search_historical_context, query, limit, None)
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc).lower()
        recoverable_error = (
            "status code: 404" in error_text
            or ("model" in error_text and "not found" in error_text)
            or "connection refused" in error_text
        )
        if recoverable_error:
            logger.warning("context_search_unavailable query=%s error=%s", query, exc)
            raise ContextSearchUnavailableError(str(exc)) from exc
        raise
    return [
        item
        for item in (_to_historical_context(match, relevance_threshold) for match in raw_results)
        if item is not None
    ]


async def run_context_hunter(page_id: str) -> dict[str, Any]:
    """Run Context Hunter for a page and append historical context toggle if relevant."""

    if not page_id.strip():
        raise ValueError("page_id is required")

    try:
        settings = get_settings()
        notion = _get_notion_client(settings)

        logger.info("context_hunter_started page_id=%s", page_id)
        page_title, page_text = await _fetch_page_snapshot(notion=notion, page_id=page_id)
        meaningful_text = _strip_context_artifacts(page_text)
        if not meaningful_text:
            comment_response = await _post_no_content_comment(notion=notion, page_id=page_id)
            logger.info("context_hunter_no_text page_id=%s", page_id)
            return {
                "ok": True,
                "status": "no_content",
                "page_id": page_id,
                "contexts_appended": 0,
                "notion_response": comment_response,
            }

        chroma = _get_chroma_manager(settings)
        try:
            search_query = await _generate_search_query(title=page_title, content=meaningful_text, settings=settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "context_query_generation_failed page_id=%s error_type=%s error=%r",
                page_id,
                type(exc).__name__,
                exc,
            )
            reason = _describe_query_generation_failure(exc)
            comment_response = await _post_query_failure_comment(notion=notion, page_id=page_id, reason=reason)
            return {
                "ok": True,
                "status": "query_generation_failed",
                "page_id": page_id,
                "contexts_appended": 0,
                "reliable": False,
                "error_reason": reason,
                "notion_response": comment_response,
            }

        logger.info("context_query_generated page_id=%s query=%s", page_id, search_query)

        try:
            contexts = await _search_relevant_contexts(
                chroma=chroma,
                query=search_query,
                limit=settings.chroma.top_k,
                relevance_threshold=settings.chroma.relevance_threshold,
            )
        except ContextSearchUnavailableError as exc:
            comment_response = await _post_search_unavailable_comment(
                notion=notion,
                page_id=page_id,
                reason=str(exc),
            )
            return {
                "ok": True,
                "status": "search_unavailable",
                "page_id": page_id,
                "query": search_query,
                "contexts_appended": 0,
                "notion_response": comment_response,
            }

        if not contexts:
            logger.info("context_hunter_no_relevant_results page_id=%s", page_id)
            comment_response = await _post_no_relevant_context_comment(
                notion=notion,
                page_id=page_id,
                query=search_query,
            )
            return {
                "ok": True,
                "status": "no_relevant_context",
                "page_id": page_id,
                "query": search_query,
                "contexts_appended": 0,
                "notion_response": comment_response,
            }

        notion_response = await append_historical_context_toggle_block(
            notion=notion,
            page_id=page_id,
            contexts=[context.model_dump(mode="json") for context in contexts],
            query=search_query,
        )
        logger.info("context_hunter_completed page_id=%s contexts_appended=%s", page_id, len(contexts))
        return {
            "ok": True,
            "status": "context_appended",
            "page_id": page_id,
            "page_title": page_title,
            "query": search_query,
            "contexts_appended": len(contexts),
            "notion_response": notion_response,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("context_hunter_failed page_id=%s error=%s", page_id, exc)
        raise RuntimeError(f"Context Hunter failed for page {page_id}: {exc}") from exc
