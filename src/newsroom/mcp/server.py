"""FastMCP server exposing newsroom tools for Notion workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Callable

import structlog

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover - compatibility path
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]

from newsroom.chroma.manager import ChromaManager
from newsroom.config import Settings, get_settings
from newsroom.notion.blocks import (
    append_historical_context_toggle_block,
    clean_markdown_for_publishing,
    clean_page_formatting_before_publishing,
    post_audit_findings_comment,
)
from newsroom.notion.client import NotionClient
from newsroom.types import AuditIssue, AuditResult, HistoricalContext, PitchIdea

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class Dependencies:
    """Container for injectable service factories and cached instances."""

    settings_provider: Callable[[], Settings]
    notion_factory: Callable[[Settings], NotionClient]
    chroma_factory: Callable[[Settings], ChromaManager]

    _settings: Settings | None = None
    _notion: NotionClient | None = None
    _chroma: ChromaManager | None = None

    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = self.settings_provider()
        return self._settings

    def notion(self) -> NotionClient:
        if self._notion is None:
            self._notion = self.notion_factory(self.settings())
        return self._notion

    def chroma(self) -> ChromaManager:
        if self._chroma is None:
            self._chroma = self.chroma_factory(self.settings())
        return self._chroma


def _default_chroma_factory(settings: Settings) -> ChromaManager:
    return ChromaManager(
        persist_directory=settings.chroma.persist_directory,
        ollama_host=str(settings.ollama.host),
        embedding_model=settings.ollama.embedding_model,
        collection_name=settings.chroma.collection_name,
    )


@lru_cache(maxsize=1)
def _get_default_dependencies() -> Dependencies:
    return Dependencies(
        settings_provider=get_settings,
        notion_factory=lambda settings: NotionClient(settings=settings),
        chroma_factory=_default_chroma_factory,
    )


_deps: Dependencies = _get_default_dependencies()


def configure_dependencies(
    *,
    settings_provider: Callable[[], Settings] | None = None,
    notion_factory: Callable[[Settings], NotionClient] | None = None,
    chroma_factory: Callable[[Settings], ChromaManager] | None = None,
) -> None:
    """Override dependency providers for tests or custom runtime wiring."""

    global _deps
    settings_fn = settings_provider or get_settings
    notion_fn = notion_factory or (lambda settings: NotionClient(settings=settings))
    chroma_fn = chroma_factory or _default_chroma_factory
    _deps = Dependencies(
        settings_provider=settings_fn,
        notion_factory=notion_fn,
        chroma_factory=chroma_fn,
    )


def _extract_plain_text_from_blocks(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if not isinstance(block_type, str):
            continue
        payload = block.get(block_type)
        if not isinstance(payload, dict):
            continue
        rich_text = payload.get("rich_text", [])
        if not isinstance(rich_text, list):
            continue
        plain = "".join(
            part.get("plain_text", "") for part in rich_text if isinstance(part, dict)
        ).strip()
        if plain:
            lines.append(plain)
    return "\n\n".join(lines).strip()


def _heuristic_audit(draft_text: str, guide_text: str | None = None) -> AuditResult:
    issues: list[AuditIssue] = []
    recommendations: list[str] = []

    words = re.findall(r"\w+", draft_text)
    word_count = len(words)

    if word_count < 250:
        issues.append(
            AuditIssue(
                category="structure",
                severity="medium",
                message="Draft is too short for a full newsroom narrative.",
                suggested_fix="Expand context and evidence sections before publication.",
            )
        )

    long_sentences = [s for s in re.split(r"(?<=[.!?])\s+", draft_text) if len(s.split()) > 34]
    if len(long_sentences) >= 3:
        issues.append(
            AuditIssue(
                category="clarity",
                severity="medium",
                message="Multiple long sentences may reduce readability.",
                suggested_fix="Split long sentences into shorter statements with explicit evidence.",
            )
        )

    hype_hits = len(re.findall(r"\b(disruptive|revolutionary|groundbreaking|unprecedented)\b", draft_text, re.I))
    if hype_hits > 2:
        issues.append(
            AuditIssue(
                category="tone",
                severity="high",
                message="Tone appears promotional instead of evidence-led.",
                suggested_fix="Replace hype terms with factual language and citations.",
            )
        )

    if guide_text and len(guide_text.split()) > 30:
        if "evidence" in guide_text.lower() and "according to" not in draft_text.lower():
            issues.append(
                AuditIssue(
                    category="brand_alignment",
                    severity="medium",
                    message="Draft may not satisfy evidence-first guidance.",
                    suggested_fix="Add at least one sourced statement in the lead section.",
                )
            )

    score = max(0, 100 - (len(issues) * 15) - (10 if word_count < 180 else 0))
    if score >= 85:
        status = "pass"
        summary = "Narrative quality is strong with only minor refinement needed."
    elif score >= 60:
        status = "needs_revision"
        summary = "Draft has potential but requires revisions before publication."
    else:
        status = "fail"
        summary = "Draft has critical narrative issues that should be addressed first."

    if issues:
        recommendations.append("Run one final copy edit pass after revisions.")
    if not recommendations:
        recommendations.append("Proceed with publication checklist and final fact verification.")

    return AuditResult(
        status=status,
        score=score,
        summary=summary,
        issues=issues,
        recommendations=recommendations,
        checked_at=datetime.now(UTC),
        tool_version="mcp-server-v1",
    )


app = FastMCP("Notion Newsroom OS")
tool = app.tool


@tool()
async def search_historical_context(
    page_id: str,
    query: str,
    limit: int = 8,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Search Chroma archive for historical context related to the given query."""

    log = logger.bind(tool="search_historical_context", page_id=page_id, limit=limit)
    try:
        if not page_id.strip() or not query.strip():
            raise ValueError("page_id and query are required")

        matches = await _deps.chroma().asearch_historical_context(
            query=query,
            limit=max(1, limit),
            filters=filters,
        )

        contexts: list[dict[str, Any]] = []
        for item in matches:
            metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            context = HistoricalContext(
                source_page_id=str(metadata.get("page_id") or "unknown"),
                title=str(metadata.get("title") or "Untitled"),
                snippet=str(item.get("document") or ""),
                score=float(item.get("score") or 0.0),
                url=metadata.get("url") or None,
                published_at=metadata.get("date") or None,
            )
            contexts.append(context.model_dump(mode="json"))

        log.info("tool_success", result_count=len(contexts))
        return contexts
    except Exception as exc:  # noqa: BLE001
        log.exception("tool_failure", error=str(exc))
        raise RuntimeError(f"search_historical_context failed: {exc}") from exc


@tool()
async def append_historical_block(
    page_id: str,
    query: str,
    limit: int = 8,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a Historical Context toggle block to a Notion page."""

    log = logger.bind(tool="append_historical_block", page_id=page_id)
    try:
        contexts = await search_historical_context(page_id=page_id, query=query, limit=limit, filters=filters)
        response = await append_historical_context_toggle_block(
            notion=_deps.notion(),
            page_id=page_id,
            contexts=contexts,
            query=query,
        )
        result = {
            "ok": True,
            "page_id": page_id,
            "query": query,
            "contexts_appended": len(contexts),
            "notion_response": response,
        }
        log.info("tool_success", contexts_appended=len(contexts))
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("tool_failure", error=str(exc))
        raise RuntimeError(f"append_historical_block failed: {exc}") from exc


@tool()
async def generate_followup_angles(
    page_id: str,
    query: str,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Generate follow-up story angles using historical context and traffic cues."""

    log = logger.bind(tool="generate_followup_angles", page_id=page_id, top_n=top_n)
    try:
        contexts = await search_historical_context(page_id=page_id, query=query, limit=max(3, top_n * 2))
        if not contexts:
            return []

        page = await _deps.notion().get_page_model(page_id)
        page_title = page.title
        ideas: list[PitchIdea] = []

        for idx, context_data in enumerate(contexts[: max(1, top_n)]):
            context = HistoricalContext.model_validate(context_data)
            angle_title = f"Follow-up: {context.title} intersects with {page_title}"
            hypothesis = (
                "Current newsroom momentum suggests a second-angle story where "
                f"{context.title.lower()} adds new evidence to the primary narrative."
            )
            rationale = (
                f"Similarity score {context.score:.2f} and archival overlap indicate audience relevance."
            )
            ideas.append(
                PitchIdea(
                    title=angle_title,
                    hypothesis=hypothesis,
                    rationale=rationale,
                    priority="high" if context.score >= 0.8 else "medium",
                    confidence=min(0.95, max(0.45, context.score)),
                    source_page_id=context.source_page_id,
                    supporting_signals=[
                        f"Historical match score: {context.score:.2f}",
                        f"Related archive title: {context.title}",
                    ],
                )
            )
            if idx + 1 >= top_n:
                break

        log.info("tool_success", generated=len(ideas))
        return [idea.model_dump(mode="json") for idea in ideas]
    except Exception as exc:  # noqa: BLE001
        log.exception("tool_failure", error=str(exc))
        raise RuntimeError(f"generate_followup_angles failed: {exc}") from exc


@tool()
async def audit_narrative(
    page_id: str,
    brand_guide_page_id: str | None = None,
    post_comment: bool = True,
) -> dict[str, Any]:
    """Audit a draft narrative and optionally post findings as a Notion comment."""

    log = logger.bind(tool="audit_narrative", page_id=page_id)
    try:
        notion = _deps.notion()
        blocks = await notion.list_block_children(page_id)
        draft_text = _extract_plain_text_from_blocks(blocks)
        if not draft_text:
            raise ValueError("Draft page has no readable text blocks")

        guide_id = brand_guide_page_id or _deps.settings().notion.brand_guide_page_id
        guide_text: str | None = None
        if guide_id:
            guide_blocks = await notion.list_block_children(guide_id)
            guide_text = _extract_plain_text_from_blocks(guide_blocks)

        audit = _heuristic_audit(draft_text=draft_text, guide_text=guide_text)
        audit.draft_page_id = page_id
        audit.brand_guide_page_id = guide_id

        comment_response: dict[str, Any] | None = None
        if post_comment:
            comment_response = await post_audit_findings_comment(notion=notion, page_id=page_id, audit=audit)

        result = {
            "ok": True,
            "audit": audit.model_dump(mode="json"),
            "comment_posted": bool(comment_response),
            "comment_response": comment_response,
        }
        log.info("tool_success", status=audit.status, score=audit.score)
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("tool_failure", error=str(exc))
        raise RuntimeError(f"audit_narrative failed: {exc}") from exc


@tool()
async def prepare_for_publication(page_id: str) -> dict[str, Any]:
    """Clean page formatting and produce publication-ready markdown preview."""

    log = logger.bind(tool="prepare_for_publication", page_id=page_id)
    try:
        notion = _deps.notion()
        cleanup_stats = await clean_page_formatting_before_publishing(notion=notion, page_id=page_id)
        blocks_after = await notion.list_block_children(page_id)
        plain_text = _extract_plain_text_from_blocks(blocks_after)
        markdown_preview = clean_markdown_for_publishing(plain_text)

        result = {
            "ok": True,
            "page_id": page_id,
            "cleanup": cleanup_stats,
            "markdown_preview": markdown_preview,
            "preview_characters": len(markdown_preview),
        }
        log.info("tool_success", cleanup=cleanup_stats)
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("tool_failure", error=str(exc))
        raise RuntimeError(f"prepare_for_publication failed: {exc}") from exc
