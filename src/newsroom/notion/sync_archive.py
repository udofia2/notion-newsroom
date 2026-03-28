"""Notion archive sync job for Chroma ingestion.

This module supports both initial bootstrap and periodic re-sync by:
- querying the main story database and archive databases
- converting page content into markdown + metadata records
- ingesting records into Chroma in batches with retries
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from newsroom.chroma.manager import ChromaManager
from newsroom.config import Settings, get_settings
from newsroom.notion.client import NotionClient

logger = logging.getLogger(__name__)

_MAX_RECURSION_DEPTH = 6


def _resolve_database_ids(settings: Settings, database_ids: list[str] | None) -> list[str]:
    if database_ids:
        return [item.strip() for item in database_ids if item.strip()]

    ids = [settings.notion.database_id]
    if settings.notion.articles_database_id:
        ids.append(settings.notion.articles_database_id)

    extra = os.getenv("ARCHIVE_DATABASE_IDS", "")
    if extra.strip():
        ids.extend(part.strip() for part in extra.split(",") if part.strip())

    seen: set[str] = set()
    unique: list[str] = []
    for db_id in ids:
        if db_id not in seen:
            seen.add(db_id)
            unique.append(db_id)
    return unique


def _extract_rich_text(block: dict[str, Any], block_type: str) -> str:
    payload = block.get(block_type)
    if not isinstance(payload, dict):
        return ""
    rich_text = payload.get("rich_text", [])
    if not isinstance(rich_text, list):
        return ""
    return "".join(part.get("plain_text", "") for part in rich_text if isinstance(part, dict)).strip()


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        return f"<li>{text}</li>" if text else ""

    if block_type == "code":
        text = _escape_html(_extract_rich_text(block, "code"))
        return f"<pre><code>{text}</code></pre>" if text else ""

    if block_type == "divider":
        return "<hr />"

    return ""


def _markdownify_html(html: str) -> str:
    try:
        markdownify_module = importlib.import_module("markdownify")
        markdownify_fn = getattr(markdownify_module, "markdownify")
        return str(markdownify_fn(html, heading_style="ATX"))
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", "", html)


def _find_title_from_page(page: dict[str, Any]) -> str:
    properties = page.get("properties", {}) if isinstance(page, dict) else {}
    if not isinstance(properties, dict):
        return "Untitled"

    for value in properties.values():
        if not isinstance(value, dict) or value.get("type") != "title":
            continue
        parts = value.get("title", [])
        if not isinstance(parts, list):
            continue
        title = "".join(part.get("plain_text", "") for part in parts if isinstance(part, dict)).strip()
        if title:
            return title
    return "Untitled"


async def _with_retry(
    task_name: str,
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    retries: int = 4,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries:
                break
            delay = base_delay * attempt
            logger.warning("sync_retry task=%s attempt=%s delay=%.1f error=%s", task_name, attempt, delay, exc)
            await asyncio.sleep(delay)
    raise RuntimeError(f"{task_name} failed after {retries} attempts: {last_error}")


async def _fetch_blocks_recursive(
    notion: NotionClient,
    block_id: str,
    depth: int = 0,
) -> list[dict[str, Any]]:
    blocks = await _with_retry("list_block_children", notion.list_block_children, block_id)
    if depth >= _MAX_RECURSION_DEPTH:
        return blocks

    out: list[dict[str, Any]] = list(blocks)
    for block in blocks:
        child_id = block.get("id")
        has_children = bool(block.get("has_children"))
        if has_children and isinstance(child_id, str):
            child_blocks = await _fetch_blocks_recursive(notion=notion, block_id=child_id, depth=depth + 1)
            out.extend(child_blocks)
    return out


def _wrap_list_items(fragments: list[str]) -> list[str]:
    out: list[str] = []
    in_list = False
    for fragment in fragments:
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


async def _build_archive_record(
    notion: NotionClient,
    page: dict[str, Any],
    database_id: str,
) -> dict[str, Any] | None:
    page_id = page.get("id")
    if not isinstance(page_id, str) or not page_id.strip():
        return None

    blocks = await _fetch_blocks_recursive(notion=notion, block_id=page_id)
    fragments = [fragment for fragment in (_block_to_html(block) for block in blocks) if fragment]
    html = "\n".join(_wrap_list_items(fragments)).strip()
    markdown = _markdownify_html(html).strip()
    if not markdown:
        return None

    return {
        "id": page_id,
        "page_id": page_id,
        "title": _find_title_from_page(page),
        "url": page.get("url"),
        "date": page.get("last_edited_time") or page.get("created_time") or datetime.now(UTC).isoformat(),
        "database_id": database_id,
        "markdown": markdown,
        "content": markdown,
    }


def _chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def sync_archive_to_chroma(
    *,
    batch_size: int = 20,
    database_ids: list[str] | None = None,
    max_retries: int = 4,
) -> dict[str, Any]:
    """Sync main story database + archives into Chroma in batches.

    This method is safe for both initial bootstrap and periodic re-sync.
    """

    settings = get_settings()
    notion = NotionClient(settings=settings)
    chroma = ChromaManager(
        persist_directory=settings.chroma.persist_directory,
        ollama_host=str(settings.ollama.host),
        embedding_model=settings.ollama.embedding_model,
        collection_name=settings.chroma.collection_name,
    )

    target_databases = _resolve_database_ids(settings=settings, database_ids=database_ids)
    logger.info("sync_archive_started databases=%s batch_size=%s", len(target_databases), batch_size)

    all_records: list[dict[str, Any]] = []
    scanned_pages = 0

    for db_id in target_databases:
        pages = await _with_retry(
            "query_database",
            notion.query_database,
            db_id,
            page_size=100,
            retries=max_retries,
        )
        scanned_pages += len(pages)
        for page in pages:
            try:
                record = await _with_retry(
                    "build_archive_record",
                    _build_archive_record,
                    notion,
                    page,
                    db_id,
                    retries=max_retries,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("sync_archive_skip_page page_id=%s error=%s", page.get("id"), exc)
                continue
            if record is not None:
                all_records.append(record)

    batches = _chunks(all_records, max(1, batch_size))
    chunk_documents_indexed = 0

    for index, batch in enumerate(batches, start=1):
        try:
            added_chunks = await _with_retry(
                "chroma_add_notion_pages",
                chroma.aadd_notion_pages,
                batch,
                retries=max_retries,
            )
            chunk_documents_indexed += int(added_chunks)
            logger.info(
                "sync_archive_batch_success index=%s total_batches=%s pages=%s chunks=%s",
                index,
                len(batches),
                len(batch),
                added_chunks,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync_archive_batch_failed index=%s error=%s", index, exc)

    result = {
        "ok": True,
        "databases_scanned": target_databases,
        "pages_scanned": scanned_pages,
        "pages_converted": len(all_records),
        "batches": len(batches),
        "chunk_documents_indexed": chunk_documents_indexed,
    }
    logger.info(
        "sync_archive_completed pages_scanned=%s pages_converted=%s indexed_chunks=%s",
        scanned_pages,
        len(all_records),
        chunk_documents_indexed,
    )
    return result


async def periodic_archive_resync() -> dict[str, Any]:
    """Convenience wrapper for scheduled recurring archive sync."""

    return await sync_archive_to_chroma(batch_size=20)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    summary = asyncio.run(sync_archive_to_chroma())
    print(summary)
