"""CSV archive ingest job for Chroma ingestion.

This module ingests external CSV article archives into Chroma for historical-context
retrieval, complementing the Notion-based sync_archive module.

Schema expectations (CSV):
- id: unique article identifier
- title: article headline
- content: article body text
- date: publication date (ISO 8601 preferred)
- backlinks: pipe-separated URLs (optional, first is used)
"""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from newsroom.chroma.manager import ChromaManager
from newsroom.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _first_backlink(backlinks: str | None) -> str:
    """Extract first URL from pipe-separated backlinks string."""
    if not backlinks:
        return ""
    parts = [part.strip() for part in backlinks.split("|") if part.strip()]
    return parts[0] if parts else ""


def _record_from_csv_row(row: dict[str, str], default_database_id: str) -> dict[str, Any] | None:
    """Convert a CSV row into a Chroma-compatible archive record.

    Required fields: id, content.
    Optional fields: title, date, backlinks.
    """
    page_id = (row.get("id") or "").strip()
    title = (row.get("title") or "Untitled").strip() or "Untitled"
    content = (row.get("content") or "").strip()
    date_value = (row.get("date") or "").strip()
    url = _first_backlink(row.get("backlinks"))

    if not page_id or not content:
        return None

    return {
        "id": page_id,
        "page_id": page_id,
        "title": title,
        "url": url,
        "date": date_value or datetime.now(UTC).isoformat(),
        "database_id": default_database_id,
        "markdown": content,
        "content": content,
    }


def _chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    """Split items into chunks of specified size."""
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _with_retry(
    task_name: str,
    func,
    *args: Any,
    retries: int = 4,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> Any:
    """Retry async function with exponential backoff."""
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


async def sync_csv_archive_to_chroma(
    *,
    csv_path: str,
    batch_size: int = 50,
    max_retries: int = 4,
    database_id: str = "csv_import",
) -> dict[str, Any]:
    """Ingest CSV archive rows into Chroma for historical-context retrieval.

    Args:
        csv_path: Path to CSV file (relative or absolute).
        batch_size: Number of records per embedding batch.
        max_retries: Max retries per batch before failing.
        database_id: Metadata tag for ingested records.

    Returns:
        Summary dict with rows_scanned, rows_converted, chunk_documents_indexed.
    """

    settings = get_settings()
    chroma = ChromaManager(
        persist_directory=settings.chroma.persist_directory,
        ollama_host=str(settings.ollama.host),
        embedding_model=settings.ollama.embedding_model,
        collection_name=settings.chroma.collection_name,
    )

    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    records: list[dict[str, Any]] = []
    rows_scanned = 0

    logger.info("sync_csv_archive_started csv_path=%s database_id=%s", csv_path, database_id)

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV file is empty or malformed")

        for row in reader:
            rows_scanned += 1
            record = _record_from_csv_row(row, default_database_id=database_id)
            if record is not None:
                records.append(record)

    batches = _chunks(records, max(1, batch_size))
    chunk_documents_indexed = 0

    logger.info(
        "sync_csv_archive_batching rows_scanned=%s rows_converted=%s batches=%s",
        rows_scanned,
        len(records),
        len(batches),
    )

    for index, batch in enumerate(batches, start=1):
        try:
            added_chunks = await _with_retry(
                "chroma_add_csv_pages",
                chroma.aadd_notion_pages,
                batch,
                retries=max_retries,
            )
            chunk_documents_indexed += int(added_chunks)
            logger.info(
                "sync_csv_archive_batch_success index=%s total_batches=%s rows=%s chunks=%s",
                index,
                len(batches),
                len(batch),
                added_chunks,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync_csv_archive_batch_failed index=%s error=%s", index, exc)
            raise

    result = {
        "ok": True,
        "csv_path": str(path),
        "database_id": database_id,
        "rows_scanned": rows_scanned,
        "rows_converted": len(records),
        "batches": len(batches),
        "chunk_documents_indexed": chunk_documents_indexed,
    }
    logger.info(
        "sync_csv_archive_completed rows_scanned=%s rows_converted=%s indexed_chunks=%s",
        rows_scanned,
        len(records),
        chunk_documents_indexed,
    )
    return result


if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO)

    csv_path = os.getenv("ARCHIVE_CSV_PATH", "").strip()
    if not csv_path:
        print("Usage: ARCHIVE_CSV_PATH=<path> python -m newsroom.notion.sync_csv_archive")
        print("Example: ARCHIVE_CSV_PATH=news-articles/cleaned_articles.csv python -m newsroom.notion.sync_csv_archive")
        sys.exit(1)

    try:
        summary = asyncio.run(sync_csv_archive_to_chroma(csv_path=csv_path))
        print(f"\n✓ Sync complete: {summary}")
    except Exception as exc:
        print(f"\n✗ Sync failed: {exc}")
        sys.exit(1)
