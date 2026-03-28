"""Agency Bridge workflow.

Cleans Notion content, prepares markdown/html payloads, pushes to webhook, and marks
content as published in Notion.
"""

from __future__ import annotations

import logging
import importlib
import os
import re
from datetime import UTC, datetime
from typing import Any

import httpx


def _markdownify_html(html: str) -> str:
    """Convert HTML to Markdown using markdownify when available."""

    try:
        markdownify_module = importlib.import_module("markdownify")
        markdownify_fn = getattr(markdownify_module, "markdownify")
        return str(markdownify_fn(html, heading_style="ATX"))
    except Exception:  # noqa: BLE001
        # Minimal fallback for local runs when markdownify is unavailable.
        return re.sub(r"<[^>]+>", "", html)

from newsroom.config import get_settings
from newsroom.notion.client import NotionClient

logger = logging.getLogger(__name__)

_PROPERTY_LINE_PATTERN = re.compile(
    r"^\s*(status|date|published|tags?|author|category|slug|database|owner)\s*:\s*",
    re.IGNORECASE,
)


def _extract_rich_text(block: dict[str, Any], block_type: str) -> str:
    payload = block.get(block_type)
    if not isinstance(payload, dict):
        return ""
    rich_text = payload.get("rich_text", [])
    if not isinstance(rich_text, list):
        return ""
    return "".join(part.get("plain_text", "") for part in rich_text if isinstance(part, dict)).strip()


def _is_property_like_paragraph(block: dict[str, Any]) -> bool:
    if block.get("type") != "paragraph":
        return False
    text = _extract_rich_text(block, "paragraph")
    return bool(text and _PROPERTY_LINE_PATTERN.match(text))


def _should_remove_block(block: dict[str, Any]) -> bool:
    block_type = str(block.get("type") or "")
    if block_type == "toggle":
        return True
    if block_type in {"child_database", "synced_block"}:
        return True
    return _is_property_like_paragraph(block)


async def _cleanup_page_blocks(notion: NotionClient, page_id: str) -> dict[str, int]:
    blocks = await notion.list_block_children(page_id)
    removed_toggles = 0
    removed_databases = 0
    removed_properties = 0

    for block in blocks:
        block_id = block.get("id")
        if not isinstance(block_id, str):
            continue
        block_type = str(block.get("type") or "")

        if not _should_remove_block(block):
            continue

        await notion.delete_block(block_id)
        if block_type == "toggle":
            removed_toggles += 1
        elif block_type in {"child_database", "synced_block"}:
            removed_databases += 1
        else:
            removed_properties += 1

    return {
        "removed_toggles": removed_toggles,
        "removed_database_blocks": removed_databases,
        "removed_property_lines": removed_properties,
    }


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _block_to_html(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "")
    if block_type in {"paragraph", "heading_1", "heading_2", "heading_3", "quote"}:
        text = _escape_html(_extract_rich_text(block, block_type))
        if not text:
            return ""
        if block_type == "paragraph":
            return f"<p>{text}</p>"
        if block_type == "heading_1":
            return f"<h1>{text}</h1>"
        if block_type == "heading_2":
            return f"<h2>{text}</h2>"
        if block_type == "heading_3":
            return f"<h3>{text}</h3>"
        return f"<blockquote>{text}</blockquote>"

    if block_type in {"bulleted_list_item", "numbered_list_item", "to_do"}:
        text = _escape_html(_extract_rich_text(block, block_type))
        if not text:
            return ""
        return f"<li>{text}</li>"

    if block_type == "code":
        text = _escape_html(_extract_rich_text(block, "code"))
        if not text:
            return ""
        return f"<pre><code>{text}</code></pre>"

    return ""


def _wrap_list_items(html_fragments: list[str]) -> list[str]:
    out: list[str] = []
    in_list = False
    for fragment in html_fragments:
        is_li = fragment.startswith("<li>")
        if is_li and not in_list:
            out.append("<ul>")
            in_list = True
        if not is_li and in_list:
            out.append("</ul>")
            in_list = False
        out.append(fragment)
    if in_list:
        out.append("</ul>")
    return out


async def _render_page_html(notion: NotionClient, page_id: str) -> str:
    blocks = await notion.list_block_children(page_id)
    fragments = [fragment for fragment in (_block_to_html(block) for block in blocks) if fragment]
    wrapped = _wrap_list_items(fragments)
    return "\n".join(wrapped).strip()


def _resolve_webhook_settings() -> tuple[str | None, str | None]:
    url = os.getenv("AGENCY_WEBHOOK_URL")
    token = os.getenv("AGENCY_WEBHOOK_TOKEN")
    return (url.strip() if url else None, token.strip() if token else None)


async def _push_webhook(
    webhook_url: str,
    webhook_token: str | None,
    payload: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if webhook_token:
        headers["Authorization"] = f"Bearer {webhook_token}"

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(webhook_url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.text[:1200]
        return {
            "status_code": response.status_code,
            "response_excerpt": body,
        }


def _find_property_name(schema: dict[str, Any], property_type: str, hints: tuple[str, ...]) -> str | None:
    candidates = [name for name, data in schema.items() if isinstance(data, dict) and data.get("type") == property_type]
    for hint in hints:
        for name in candidates:
            if hint in name.lower():
                return name
    return candidates[0] if candidates else None


async def _mark_as_published(notion: NotionClient, page_id: str) -> dict[str, Any]:
    page = await notion.get_page(page_id)
    schema = page.get("properties", {}) if isinstance(page, dict) else {}
    schema = schema if isinstance(schema, dict) else {}

    properties: dict[str, Any] = {}
    status_name = _find_property_name(schema, "status", ("status",))
    if status_name:
        properties[status_name] = {"status": {"name": "Published"}}

    date_name = _find_property_name(schema, "date", ("published", "publish", "date"))
    if date_name:
        properties[date_name] = {"date": {"start": datetime.now(UTC).isoformat()}}

    if not properties:
        return {"updated": False, "reason": "No status/date properties detected"}

    await notion.update_page(page_id=page_id, properties=properties)
    return {"updated": True, "properties_updated": list(properties.keys())}


async def prepare_for_publication(page_id: str, include_html: bool = True) -> dict[str, Any]:
    """Clean a page, convert to markdown/html, optionally push to CMS, and mark published."""

    if not page_id.strip():
        raise ValueError("page_id is required")

    settings = get_settings()
    notion = NotionClient(settings=settings)
    logger.info("agency_bridge_started page_id=%s", page_id)

    cleanup = await _cleanup_page_blocks(notion=notion, page_id=page_id)
    html = await _render_page_html(notion=notion, page_id=page_id)
    markdown = _markdownify_html(html) if html else ""

    page_model = await notion.get_page_model(page_id)
    webhook_url, webhook_token = _resolve_webhook_settings()
    payload = {
        "page_id": page_id,
        "title": page_model.title,
        "source_url": str(page_model.url) if page_model.url else None,
        "markdown": markdown,
        "html": html if include_html else None,
        "metadata": {
            "cleanup": cleanup,
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }

    webhook_result: dict[str, Any] | None = None
    if webhook_url:
        try:
            webhook_result = await _push_webhook(
                webhook_url=webhook_url,
                webhook_token=webhook_token,
                payload=payload,
                timeout_seconds=settings.request_timeout_seconds,
            )
            logger.info("agency_bridge_webhook_success page_id=%s status=%s", page_id, webhook_result["status_code"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("agency_bridge_webhook_failed page_id=%s error=%s", page_id, exc)
            raise RuntimeError(f"Webhook push failed for page {page_id}: {exc}") from exc

    publish_update = await _mark_as_published(notion=notion, page_id=page_id)

    # Notion comments are not block children and are excluded from export by design.
    result = {
        "ok": True,
        "page_id": page_id,
        "title": page_model.title,
        "markdown": markdown,
        "html": html if include_html else None,
        "metadata": {
            "cleanup": cleanup,
            "comments_removed": "not_applicable_comments_are_not_body_blocks",
            "webhook_called": bool(webhook_url),
            "webhook_result": webhook_result,
            "publish_update": publish_update,
        },
    }
    logger.info("agency_bridge_completed page_id=%s", page_id)
    return result
