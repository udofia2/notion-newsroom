"""Helpers for composing and mutating Notion blocks for newsroom workflows."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from newsroom.notion.client import NotionClient
from newsroom.types import AuditResult, HistoricalContext

_MAX_RICH_TEXT_CHARS = 1800


def _text_rich_text(content: str, bold: bool = False, italic: bool = False) -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": content},
        "annotations": {"bold": bold, "italic": italic},
    }


def _text_link_rich_text(content: str, url: str, bold: bool = False, italic: bool = False) -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": content, "link": {"url": url}},
        "annotations": {"bold": bold, "italic": italic},
    }


def _page_mention_rich_text(page_id: str) -> dict[str, Any]:
    return {
        "type": "mention",
        "mention": {
            "type": "page",
            "page": {"id": page_id},
        },
    }


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _context_source_rich_text(context: HistoricalContext) -> dict[str, Any]:
    if _is_uuid(context.source_page_id):
        return _page_mention_rich_text(context.source_page_id)
    if context.url:
        return _text_link_rich_text(context.source_page_id, str(context.url))
    return _text_rich_text(context.source_page_id)


def _split_for_rich_text(text: str, max_chars: int = _MAX_RICH_TEXT_CHARS) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    return [cleaned[i : i + max_chars] for i in range(0, len(cleaned), max_chars)]


def build_historical_context_toggle_block(
    contexts: list[HistoricalContext | dict[str, Any]],
    query: str | None = None,
) -> dict[str, Any]:
    """Build a toggle block that links related historical pages and snippets."""

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    title = "Historical Context"
    if query:
        title = f"Historical Context for: {query}"

    child_blocks: list[dict[str, Any]] = []
    for item in contexts:
        context = item if isinstance(item, HistoricalContext) else HistoricalContext.model_validate(item)
        rich_text: list[dict[str, Any]] = [
            _text_rich_text("Related: ", bold=True),
            _context_source_rich_text(context),
            _text_rich_text(f" | score={context.score:.2f}"),
        ]
        if context.title:
            rich_text.append(_text_rich_text(f" | {context.title}"))

        snippet_chunks = _split_for_rich_text(context.snippet)
        paragraph_rich_text = [_text_rich_text(chunk) for chunk in snippet_chunks]

        child_blocks.append(
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": rich_text,
                    "children": [
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {"rich_text": paragraph_rich_text},
                        }
                    ],
                },
            }
        )

    if not child_blocks:
        child_blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        _text_rich_text("No relevant historical context was found.", italic=True)
                    ]
                },
            }
        )

    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [
                _text_rich_text(title, bold=True),
                _text_rich_text(f"  ({now})", italic=True),
            ],
            "children": child_blocks,
        },
    }


async def append_historical_context_toggle_block(
    notion: NotionClient,
    page_id: str,
    contexts: list[HistoricalContext | dict[str, Any]],
    query: str | None = None,
) -> dict[str, Any]:
    """Append a historical context toggle section to a Notion page."""

    block = build_historical_context_toggle_block(contexts=contexts, query=query)
    return await notion.append_block_children(page_id, [block])


def format_audit_result_as_comment(audit: AuditResult) -> str:
    """Format AuditResult into a concise comment body suitable for Notion."""

    lines = [
        "Narrative Audit Results",
        f"Status: {audit.status}",
        f"Score: {audit.score}/100",
        "",
        audit.summary,
    ]

    if audit.issues:
        lines.append("")
        lines.append("Issues:")
        for issue in audit.issues:
            line = f"- [{issue.severity}] {issue.category}: {issue.message}"
            lines.append(line)
            if issue.suggested_fix:
                lines.append(f"  Suggested fix: {issue.suggested_fix}")

    if audit.recommendations:
        lines.append("")
        lines.append("Recommendations:")
        for recommendation in audit.recommendations:
            lines.append(f"- {recommendation}")

    lines.append("")
    lines.append(f"Checked at: {audit.checked_at.isoformat()}")
    lines.append(f"Tool version: {audit.tool_version}")
    return "\n".join(lines)


async def post_audit_findings_comment(
    notion: NotionClient,
    page_id: str,
    audit: AuditResult | dict[str, Any],
) -> dict[str, Any]:
    """Post formatted audit findings as one or more Notion comments."""

    audit_result = audit if isinstance(audit, AuditResult) else AuditResult.model_validate(audit)
    body = format_audit_result_as_comment(audit_result)
    chunks = _split_for_rich_text(body)

    latest_response: dict[str, Any] = {}
    for idx, chunk in enumerate(chunks):
        prefix = "" if idx == 0 else f"(continued {idx + 1}/{len(chunks)})\n"
        latest_response = await notion.create_comment(
            page_id=page_id,
            rich_text=[_text_rich_text(prefix + chunk)],
        )
    return latest_response


def format_sentence_audit_comments(
    summary: str,
    findings: list[dict[str, Any]],
) -> list[str]:
    """Build professional sentence-level narrative comments."""

    lines: list[str] = [
        "Narrative Audit Highlights",
        "",
        summary.strip(),
    ]

    if not findings:
        lines.extend(["", "No problematic sentences detected in this pass."])
        return ["\n".join(lines)]

    lines.extend(["", "Problematic Sentences & Suggestions:"])
    for idx, finding in enumerate(findings, start=1):
        sentence = str(finding.get("sentence") or "").strip()
        issue = str(finding.get("issue") or "").strip()
        suggestion = str(finding.get("suggestion") or "").strip()
        category = str(finding.get("category") or "general").strip()
        severity = str(finding.get("severity") or "medium").strip()

        lines.append(f"{idx}. [{severity}] [{category}] {issue or 'Needs revision.'}")
        if sentence:
            lines.append(f"   Sentence: \"{sentence}\"")
        if suggestion:
            lines.append(f"   Suggestion: {suggestion}")

    body = "\n".join(lines)
    return _split_for_rich_text(body)


async def post_sentence_level_audit_comments(
    notion: NotionClient,
    page_id: str,
    summary: str,
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Post sentence-level audit findings as one or more Notion comments."""

    chunks = format_sentence_audit_comments(summary=summary, findings=findings)
    responses: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks):
        prefix = "" if idx == 0 else f"(continued {idx + 1}/{len(chunks)})\n"
        response = await notion.create_comment(
            page_id=page_id,
            rich_text=[_text_rich_text(prefix + chunk)],
        )
        responses.append(response)
    return responses


def clean_markdown_for_publishing(markdown_text: str) -> str:
    """Normalize markdown to reduce formatting noise before outbound publishing."""

    text = markdown_text.replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)

    # Keep only one top-level heading to avoid malformed publisher layouts.
    seen_h1 = False
    cleaned_lines: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if line.startswith("# "):
            if seen_h1:
                line = "## " + line[2:].lstrip()
            seen_h1 = True
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n"


async def clean_page_formatting_before_publishing(
    notion: NotionClient,
    page_id: str,
    *,
    archive_empty_paragraphs: bool = True,
) -> dict[str, int]:
    """Apply lightweight in-place cleanup to a Notion page before publishing.

    Current cleanup pass:
    - archives empty paragraph blocks
    - demotes additional H1 blocks to H2 to keep a single canonical title
    """

    children = await notion.list_block_children(page_id)
    removed = 0
    demoted = 0
    seen_h1 = False

    for block in children:
        block_id = block.get("id")
        block_type = block.get("type")
        if not isinstance(block_id, str) or not isinstance(block_type, str):
            continue

        if block_type == "paragraph" and archive_empty_paragraphs:
            paragraph = block.get("paragraph", {})
            rich_text = paragraph.get("rich_text", []) if isinstance(paragraph, dict) else []
            plain = "".join(
                part.get("plain_text", "") for part in rich_text if isinstance(part, dict)
            ).strip()
            if not plain:
                await notion.delete_block(block_id)
                removed += 1
                continue

        if block_type == "heading_1":
            if seen_h1:
                heading = block.get("heading_1", {})
                rich_text = heading.get("rich_text", []) if isinstance(heading, dict) else []
                await notion.update_block(
                    block_id,
                    {
                        "heading_2": {
                            "rich_text": rich_text,
                            "is_toggleable": False,
                        }
                    },
                )
                demoted += 1
            else:
                seen_h1 = True

    return {"removed_empty_paragraphs": removed, "demoted_h1_blocks": demoted}
