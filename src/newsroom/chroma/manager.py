"""ChromaDB manager for archival retrieval over Notion newsroom content."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb
from chromadb.api.models.Collection import Collection
from ollama import AsyncClient as AsyncOllamaClient
from ollama import Client as OllamaClient

from newsroom.config import get_settings

logger = logging.getLogger(__name__)


class RecursiveArticleTextSplitter:
    """Recursive article-aware chunking tuned for long-form newsroom writing.

    The splitter approximates token counts from characters and recursively splits text
    using semantic separators first (headings, paragraphs, sentences), then falls back
    to punctuation and finally hard windows.
    """

    def __init__(
        self,
        min_tokens: int = 800,
        target_tokens: int = 1000,
        max_tokens: int = 1200,
        overlap_tokens: int = 120,
    ) -> None:
        if not (min_tokens <= target_tokens <= max_tokens):
            raise ValueError("min_tokens <= target_tokens <= max_tokens must hold")
        self.min_tokens = min_tokens
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    @staticmethod
    def _token_estimate(text: str) -> int:
        # Approximate OpenAI/Ollama token counts conservatively for English prose.
        return max(1, math.ceil(len(text) / 4))

    def _split_recursive(self, text: str, separators: list[str], max_chars: int) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if len(text) <= max_chars:
            return [text]
        if not separators:
            return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]

        sep = separators[0]
        pieces = re.split(sep, text) if sep else [text]
        if len(pieces) == 1:
            return self._split_recursive(text, separators[1:], max_chars)

        chunks: list[str] = []
        current = ""
        for piece in pieces:
            candidate = (current + " " + piece).strip() if current else piece.strip()
            if not candidate:
                continue
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.extend(self._split_recursive(current, separators[1:], max_chars))
            current = piece.strip()
        if current:
            chunks.extend(self._split_recursive(current, separators[1:], max_chars))
        return chunks

    def split(self, text: str) -> list[str]:
        if not text or not text.strip():
            return []

        target_chars = self.target_tokens * 4
        min_chars = self.min_tokens * 4
        max_chars = self.max_tokens * 4
        overlap_chars = self.overlap_tokens * 4

        separators = [
            r"\n#{1,6}\s",    # markdown headings
            r"\n\n+",         # paragraph boundaries
            r"(?<=[.!?])\s+",  # sentence boundaries
            r"(?<=[;:])\s+",   # sub-sentence boundaries
            r"\s+",            # whitespace fallback
            "",                 # hard split fallback
        ]

        base_chunks = self._split_recursive(text, separators, max_chars)
        merged: list[str] = []
        buffer = ""

        for part in base_chunks:
            candidate = (buffer + "\n\n" + part).strip() if buffer else part
            if len(candidate) < min_chars:
                buffer = candidate
                continue
            if len(candidate) <= max_chars:
                merged.append(candidate)
                buffer = ""
                continue

            if buffer:
                merged.append(buffer)
            if len(part) <= max_chars:
                merged.append(part)
            else:
                for i in range(0, len(part), target_chars):
                    merged.append(part[i : i + max_chars].strip())
            buffer = ""

        if buffer:
            if merged and len(buffer) < min_chars:
                merged[-1] = (merged[-1] + "\n\n" + buffer).strip()
            else:
                merged.append(buffer)

        # Add overlap across neighboring chunks to preserve context continuity.
        with_overlap: list[str] = []
        for idx, chunk in enumerate(merged):
            if idx == 0:
                with_overlap.append(chunk)
                continue
            prefix = merged[idx - 1][-overlap_chars:]
            with_overlap.append((prefix + "\n\n" + chunk).strip())

        return [item for item in with_overlap if item]


class ChromaManager:
    """Manager for storing and searching newsroom historical context in ChromaDB."""

    def __init__(
        self,
        persist_directory: str | None = None,
        ollama_host: str | None = None,
        embedding_model: str = "nomic-embed-text:v1.5",
        collection_name: str = "notion_newsroom_archive",
    ) -> None:
        settings = get_settings()
        resolved_persist_dir = persist_directory or settings.chroma.persist_directory
        resolved_host = ollama_host or str(settings.ollama.host)

        self.persist_directory = Path(resolved_persist_dir)
        self.persist_directory.mkdir(parents=True, exist_ok=True)
        self.embedding_model = embedding_model or settings.ollama.embedding_model
        self.collection_name = collection_name

        self._sync_ollama = OllamaClient(host=resolved_host)
        self._async_ollama = AsyncOllamaClient(host=resolved_host)
        self._chroma_client = chromadb.PersistentClient(path=str(self.persist_directory))
        self._splitter = RecursiveArticleTextSplitter()
        self._collection = self._ensure_collection()

    def _ensure_collection(self) -> Collection:
        return self._chroma_client.get_or_create_collection(name=self.collection_name)

    @staticmethod
    def _coerce_page_text(page: dict[str, Any]) -> str:
        candidates = [
            page.get("content"),
            page.get("markdown"),
            page.get("text"),
            page.get("body"),
            page.get("plain_text"),
            page.get("summary"),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    @staticmethod
    def _coerce_metadata(page: dict[str, Any]) -> dict[str, Any]:
        title = page.get("title") or page.get("name") or "Untitled"
        page_id = page.get("page_id") or page.get("id")
        if not page_id:
            raise ValueError("Each page must include 'page_id' or 'id'")

        date_value = page.get("date") or page.get("published_at") or page.get("created_time")
        if isinstance(date_value, datetime):
            iso_date = date_value.isoformat()
        elif isinstance(date_value, str) and date_value.strip():
            iso_date = date_value.strip()
        else:
            iso_date = datetime.utcnow().isoformat()

        return {
            "page_id": str(page_id),
            "title": str(title),
            "url": str(page.get("url") or ""),
            "date": iso_date,
            "database_id": str(page.get("database_id") or page.get("parent_database_id") or ""),
        }

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        response = self._sync_ollama.embed(model=self.embedding_model, input=texts)
        embeddings = response.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise RuntimeError("Failed to receive embeddings from Ollama")
        return embeddings

    async def _embed_async(self, texts: list[str]) -> list[list[float]]:
        response = await self._async_ollama.embed(model=self.embedding_model, input=texts)
        embeddings = response.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise RuntimeError("Failed to receive embeddings from Ollama")
        return embeddings

    def add_notion_pages(self, pages: list[dict[str, Any]]) -> int:
        """Chunk, embed, and store Notion pages with metadata in Chroma.

        Required metadata keys written per chunk: page_id, title, url, date, database_id.
        """

        if not pages:
            return 0

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for page in pages:
            text = self._coerce_page_text(page)
            if not text:
                logger.warning("Skipping page without text content", extra={"page": page.get("id")})
                continue

            metadata = self._coerce_metadata(page)
            chunks = self._splitter.split(text)
            for index, chunk in enumerate(chunks):
                ids.append(f"{metadata['page_id']}::chunk::{index}")
                documents.append(chunk)
                metadatas.append({**metadata, "chunk_index": index})

        if not documents:
            return 0

        embeddings = self._embed_sync(documents)
        self._collection.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
        return len(documents)

    async def aadd_notion_pages(self, pages: list[dict[str, Any]]) -> int:
        """Async variant of add_notion_pages."""

        if not pages:
            return 0

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for page in pages:
            text = self._coerce_page_text(page)
            if not text:
                continue
            metadata = self._coerce_metadata(page)
            chunks = self._splitter.split(text)
            for index, chunk in enumerate(chunks):
                ids.append(f"{metadata['page_id']}::chunk::{index}")
                documents.append(chunk)
                metadatas.append({**metadata, "chunk_index": index})

        if not documents:
            return 0

        embeddings = await self._embed_async(documents)
        await asyncio.to_thread(
            self._collection.upsert,
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        return len(documents)

    def search_historical_context(
        self,
        query: str,
        limit: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search archived newsroom chunks and return context-rich matches."""

        if not query.strip():
            return []

        query_embedding = self._embed_sync([query])[0]
        response = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=max(1, limit),
            where=filters,
        )
        return self._normalize_query_response(response)

    async def asearch_historical_context(
        self,
        query: str,
        limit: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Async variant of search_historical_context."""

        if not query.strip():
            return []

        query_embedding = (await self._embed_async([query]))[0]
        response = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=[query_embedding],
            n_results=max(1, limit),
            where=filters,
        )
        return self._normalize_query_response(response)

    def delete_page(self, page_id: str) -> int:
        """Delete all chunks associated with a page_id."""

        if not page_id.strip():
            return 0

        existing = self._collection.get(where={"page_id": page_id}, include=[])  # type: ignore[arg-type]
        ids = existing.get("ids", []) if isinstance(existing, dict) else []
        if ids:
            self._collection.delete(where={"page_id": page_id})
        return len(ids)

    async def adelete_page(self, page_id: str) -> int:
        """Async variant of delete_page."""

        if not page_id.strip():
            return 0

        existing = await asyncio.to_thread(
            self._collection.get,
            where={"page_id": page_id},
            include=[],
        )
        ids = existing.get("ids", []) if isinstance(existing, dict) else []
        if ids:
            await asyncio.to_thread(self._collection.delete, where={"page_id": page_id})
        return len(ids)

    @staticmethod
    def _normalize_query_response(response: dict[str, Any]) -> list[dict[str, Any]]:
        ids = response.get("ids", [[]])
        documents = response.get("documents", [[]])
        metadatas = response.get("metadatas", [[]])
        distances = response.get("distances", [[]])

        out: list[dict[str, Any]] = []
        first_ids = ids[0] if ids else []
        first_docs = documents[0] if documents else []
        first_meta = metadatas[0] if metadatas else []
        first_distances = distances[0] if distances else []

        for index, chunk_id in enumerate(first_ids):
            distance = float(first_distances[index]) if index < len(first_distances) else 1.0
            similarity = 1.0 / (1.0 + max(distance, 0.0))
            metadata = first_meta[index] if index < len(first_meta) and first_meta[index] else {}
            document = first_docs[index] if index < len(first_docs) else ""
            out.append(
                {
                    "id": chunk_id,
                    "score": round(similarity, 6),
                    "distance": distance,
                    "document": document,
                    "metadata": metadata,
                }
            )
        return out
