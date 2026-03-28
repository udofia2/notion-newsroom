"""Async Notion API wrapper for Newsroom OS workflows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from notion_client import APIResponseError
from notion_client import AsyncClient as NotionAsyncClient

from newsroom.config import Settings, get_settings
from newsroom.types import NotionPage

logger = logging.getLogger(__name__)


class NotionClient:
    """High-level async wrapper around notion-client for newsroom operations."""

    def __init__(self, settings: Settings | None = None, max_retries: int = 3) -> None:
        resolved_settings = settings or get_settings()
        self.settings = resolved_settings
        self.max_retries = max(1, max_retries)
        self._client = NotionAsyncClient(auth=resolved_settings.notion.token)

    async def query_database(
        self,
        database_id: str,
        *,
        filter_payload: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        """Query all pages in a Notion database with cursor pagination."""

        results: list[dict[str, Any]] = []
        next_cursor: str | None = None

        while True:
            payload: dict[str, Any] = {"database_id": database_id, "page_size": min(page_size, 100)}
            if next_cursor:
                payload["start_cursor"] = next_cursor
            if filter_payload:
                payload["filter"] = filter_payload
            if sorts:
                payload["sorts"] = sorts

            response = await self._query_with_fallback(**payload)
            items = response.get("results", [])
            if isinstance(items, list):
                results.extend(items)

            has_more = bool(response.get("has_more"))
            next_cursor = response.get("next_cursor")
            if not has_more or not next_cursor:
                break

        return results

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """Retrieve a raw Notion page object."""

        return await self._with_retry(self._client.pages.retrieve, page_id=page_id)

    async def get_page_model(self, page_id: str) -> NotionPage:
        """Retrieve a page and coerce it into a shared NotionPage model."""

        page = await self.get_page(page_id)
        return self._to_notion_page(page)

    async def list_block_children(self, block_id: str, page_size: int = 100) -> list[dict[str, Any]]:
        """List all children for a block/page with pagination."""

        children: list[dict[str, Any]] = []
        next_cursor: str | None = None

        while True:
            response = await self._with_retry(
                self._client.blocks.children.list,
                block_id=block_id,
                page_size=min(page_size, 100),
                start_cursor=next_cursor,
            )
            items = response.get("results", [])
            if isinstance(items, list):
                children.extend(items)

            has_more = bool(response.get("has_more"))
            next_cursor = response.get("next_cursor")
            if not has_more or not next_cursor:
                break

        return children

    async def append_block_children(self, block_id: str, children: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """Append children to a block/page, batching by Notion API limits."""

        if not children:
            return {"results": []}

        chunks = [children[i : i + 100] for i in range(0, len(children), 100)]
        last_response: dict[str, Any] = {"results": []}
        for chunk in chunks:
            last_response = await self._with_retry(
                self._client.blocks.children.append,
                block_id=block_id,
                children=list(chunk),
            )
        return last_response

    async def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        """Update page properties."""

        return await self._with_retry(self._client.pages.update, page_id=page_id, properties=properties)

    async def update_context_score(self, page_id: str, score: int) -> dict[str, Any]:
        """Update the page Context Score smart property."""

        normalized_score = max(0, min(100, int(score)))
        page = await self.get_page(page_id)
        properties_schema = page.get("properties", {}) if isinstance(page, dict) else {}
        if not isinstance(properties_schema, dict):
            raise RuntimeError("Page properties schema is unavailable")

        property_name = self._find_property_name(
            properties=properties_schema,
            expected_types=("number",),
            hints=("context score", "context", "relevance score", "score"),
        )
        if property_name is None:
            raise RuntimeError("Could not find a numeric Context Score property on page")

        return await self.update_page(
            page_id=page_id,
            properties={property_name: {"number": normalized_score}},
        )

    async def update_traffic_heatmap(self, page_id: str, emoji: str) -> dict[str, Any]:
        """Update the page Traffic Heatmap smart property with emoji value."""

        normalized_emoji = emoji.strip() or "🧊"
        page = await self.get_page(page_id)
        properties_schema = page.get("properties", {}) if isinstance(page, dict) else {}
        if not isinstance(properties_schema, dict):
            raise RuntimeError("Page properties schema is unavailable")

        property_name = self._find_property_name(
            properties=properties_schema,
            expected_types=("select", "rich_text", "status", "text"),
            hints=("traffic heatmap", "heatmap", "traffic", "heat"),
        )
        if property_name is None:
            raise RuntimeError("Could not find a Traffic Heatmap property on page")

        property_type = self._get_property_type(properties_schema, property_name)
        if property_type == "select":
            payload = {"select": {"name": normalized_emoji}}
        elif property_type == "status":
            payload = {"status": {"name": normalized_emoji}}
        else:
            payload = {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": normalized_emoji},
                    }
                ]
            }

        return await self.update_page(page_id=page_id, properties={property_name: payload})

    async def update_audience_persona(self, page_id: str, persona: str) -> dict[str, Any]:
        """Update the page Audience Persona smart property."""

        normalized_persona = persona.strip()
        if not normalized_persona:
            raise ValueError("persona cannot be empty")

        page = await self.get_page(page_id)
        properties_schema = page.get("properties", {}) if isinstance(page, dict) else {}
        if not isinstance(properties_schema, dict):
            raise RuntimeError("Page properties schema is unavailable")

        property_name = self._find_property_name(
            properties=properties_schema,
            expected_types=("select", "multi_select", "rich_text", "text"),
            hints=("audience persona", "persona", "audience"),
        )
        if property_name is None:
            raise RuntimeError("Could not find an Audience Persona property on page")

        property_type = self._get_property_type(properties_schema, property_name)
        if property_type == "select":
            payload = {"select": {"name": normalized_persona}}
        elif property_type == "multi_select":
            payload = {"multi_select": [{"name": normalized_persona}]}
        else:
            payload = {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": normalized_persona},
                    }
                ]
            }

        return await self.update_page(page_id=page_id, properties={property_name: payload})

    async def create_page(
        self,
        *,
        parent: dict[str, Any],
        properties: dict[str, Any],
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a page in Notion, typically under a database parent."""

        payload: dict[str, Any] = {
            "parent": parent,
            "properties": properties,
        }
        if children:
            payload["children"] = children
        return await self._with_retry(self._client.pages.create, **payload)

    async def get_database(self, database_id: str) -> dict[str, Any]:
        """Retrieve a Notion database schema and metadata."""

        return await self._with_retry(self._client.databases.retrieve, database_id=database_id)

    async def update_block(self, block_id: str, block_payload: dict[str, Any]) -> dict[str, Any]:
        """Update a block object (for example heading level or rich text)."""

        return await self._with_retry(self._client.blocks.update, block_id=block_id, **block_payload)

    async def delete_block(self, block_id: str) -> dict[str, Any]:
        """Archive (delete) a block from Notion."""

        return await self._with_retry(self._client.blocks.delete, block_id=block_id)

    async def create_comment(self, page_id: str, rich_text: list[dict[str, Any]]) -> dict[str, Any]:
        """Create a page-scoped Notion comment with rich text payload."""

        payload = {"parent": {"page_id": page_id}, "rich_text": rich_text}
        return await self._with_retry(self._client.comments.create, **payload)

    async def _query_with_fallback(self, **payload: Any) -> dict[str, Any]:
        """Query database with fallback for API endpoint migration (databases.query → data_sources.query).
        
        The new API (data_sources.query) uses data_source_id, while the old used database_id.
        Both refer to the same database ID value; we remap as needed.
        """

        # Try new API (data_sources.query) first
        try:
            new_payload = {**payload}
            if "database_id" in new_payload:
                new_payload["data_source_id"] = new_payload.pop("database_id")
            return await self._with_retry(self._client.data_sources.query, **new_payload)
        except AttributeError:
            pass

        # Fall back to old API (databases.query)
        return await self._with_retry(self._client.databases.query, **payload)

    async def _with_retry(self, func: Any, /, **kwargs: Any) -> dict[str, Any]:
        """Retry transient API failures with linear backoff."""

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return await func(**kwargs)
            except APIResponseError as exc:
                last_error = exc
                retryable = exc.code in {"rate_limited", "internal_server_error", "service_unavailable"}
                if not retryable or attempt >= self.max_retries:
                    raise
                await asyncio.sleep(attempt)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= self.max_retries:
                    raise
                await asyncio.sleep(attempt)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Unexpected retry flow in Notion client")

    @staticmethod
    def _extract_title(properties: dict[str, Any]) -> str:
        for property_value in properties.values():
            if not isinstance(property_value, dict):
                continue
            if property_value.get("type") != "title":
                continue
            title_parts = property_value.get("title", [])
            if not isinstance(title_parts, list):
                continue
            text = "".join(part.get("plain_text", "") for part in title_parts if isinstance(part, dict)).strip()
            if text:
                return text
        return "Untitled"

    @staticmethod
    def _get_property_type(properties: dict[str, Any], property_name: str) -> str:
        prop = properties.get(property_name)
        if isinstance(prop, dict):
            prop_type = prop.get("type")
            if isinstance(prop_type, str):
                return prop_type
        return ""

    @classmethod
    def _find_property_name(
        cls,
        *,
        properties: dict[str, Any],
        expected_types: tuple[str, ...],
        hints: tuple[str, ...],
    ) -> str | None:
        names = [
            name
            for name, value in properties.items()
            if isinstance(value, dict) and str(value.get("type") or "") in expected_types
        ]
        if not names:
            return None

        lowered = [(name, name.lower()) for name in names]
        for hint in hints:
            hint_lower = hint.lower()
            for name, lower_name in lowered:
                if hint_lower in lower_name:
                    return name
        return names[0]

    @classmethod
    def _to_notion_page(cls, payload: dict[str, Any]) -> NotionPage:
        properties = payload.get("properties", {})
        created_time = payload.get("created_time")
        last_edited_time = payload.get("last_edited_time")
        return NotionPage(
            id=str(payload.get("id", "")),
            title=cls._extract_title(properties if isinstance(properties, dict) else {}),
            url=payload.get("url"),
            created_time=datetime.fromisoformat(created_time.replace("Z", "+00:00"))
            if isinstance(created_time, str)
            else None,
            last_edited_time=datetime.fromisoformat(last_edited_time.replace("Z", "+00:00"))
            if isinstance(last_edited_time, str)
            else None,
            properties=properties if isinstance(properties, dict) else {},
        )
