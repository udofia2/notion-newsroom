"""Traffic Strategist workflow.

Detects trending stories, generates follow-up Angle 2 ideas, and creates pitch pages.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from newsroom.analytics.google import fetch_realtime_story_views
from newsroom.config import Settings, get_settings
from newsroom.llm import generate_text
from newsroom.notion.client import NotionClient

logger = logging.getLogger(__name__)


def _extract_plain_text(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        payload = block.get(block_type) if isinstance(block_type, str) else None
        if not isinstance(payload, dict):
            continue
        rich_text = payload.get("rich_text", [])
        if not isinstance(rich_text, list):
            continue
        text = "".join(part.get("plain_text", "") for part in rich_text if isinstance(part, dict)).strip()
        if text:
            lines.append(text)
    return "\n\n".join(lines).strip()


def _normalize_angle(angle: dict[str, Any], fallback_index: int) -> dict[str, Any]:
    title = str(angle.get("title") or f"Angle 2 idea {fallback_index}").strip()
    hypothesis = str(angle.get("hypothesis") or "Follow-up coverage opportunity detected.").strip()
    rationale = str(angle.get("rationale") or "Based on current audience momentum.").strip()
    priority = str(angle.get("priority") or "medium").lower()
    if priority not in {"low", "medium", "high", "urgent"}:
        priority = "medium"
    return {
        "title": title[:180],
        "hypothesis": hypothesis[:500],
        "rationale": rationale[:500],
        "priority": priority,
    }


def _fallback_angles(headline: str, summary: str) -> list[dict[str, Any]]:
    base = headline.strip() or "Current story"
    summary_hint = summary.strip()[:180] if summary else "Recent audience activity suggests deeper follow-up value."
    return [
        {
            "title": f"Angle 2: What changed since {base}?",
            "hypothesis": "A new development has shifted the original narrative in measurable ways.",
            "rationale": summary_hint,
            "priority": "high",
        },
        {
            "title": f"Angle 2: The hidden operators behind {base}",
            "hypothesis": "Secondary actors are driving outcomes more than headline players.",
            "rationale": "Follow the incentives, partnerships, and execution layer behind the story.",
            "priority": "medium",
        },
        {
            "title": f"Angle 2: The downstream impact of {base}",
            "hypothesis": "First-order effects are clear, but second-order consequences are underreported.",
            "rationale": "Frame the next piece around practical effects for users, teams, and markets.",
            "priority": "medium",
        },
    ]


async def _generate_angles(
    settings: Settings,
    headline: str,
    summary: str,
) -> list[dict[str, Any]]:
    prompt = (
        "You are an editorial strategist in a digital newsroom. "
        "Generate exactly 3 strong 'Angle 2' follow-up pitch ideas.\n"
        "Return valid JSON only in this format:\n"
        "{\"angles\":[{\"title\":\"...\",\"hypothesis\":\"...\",\"rationale\":\"...\",\"priority\":\"high|medium|low\"}]}\n"
        "Keep each title under 16 words and avoid generic phrasing.\n\n"
        f"Headline: {headline}\n"
        f"Summary: {summary[:1400]}\n"
    )
    timeout_seconds = min(60, settings.request_timeout_seconds)
    try:
        raw = await generate_text(settings=settings, prompt=prompt, timeout_seconds=timeout_seconds)
    except httpx.ReadTimeout:
        logger.warning(
            "angle_generation_model_timeout timeout_seconds=%s",
            timeout_seconds,
        )
        raise
    if not raw:
        raise ValueError("Model returned empty response")

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    json_blob = match.group(0) if match else raw
    parsed = json.loads(json_blob)
    angles = parsed.get("angles", []) if isinstance(parsed, dict) else []
    if not isinstance(angles, list):
        return []

    normalized = [_normalize_angle(item, idx + 1) for idx, item in enumerate(angles[:3]) if isinstance(item, dict)]
    return normalized


def _detect_threshold_crossings(rows: list[dict[str, Any]], threshold: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        views = int(row.get("views", 0) or 0)
        previous = int(row.get("previous_views", 0) or 0)
        if views >= threshold and previous < threshold:
            out.append({**row, "crossed_threshold": True})
    return out


async def detect_trending_stories(threshold: int = 10000) -> list[dict[str, Any]]:
    """Return pages whose latest views crossed the configured threshold."""

    if threshold <= 0:
        raise ValueError("threshold must be greater than zero")

    settings = get_settings()
    logger.info("traffic_detect_started threshold=%s", threshold)
    rows = await fetch_realtime_story_views(settings=settings, limit=100)
    trending = _detect_threshold_crossings(rows, threshold)
    logger.info("traffic_detect_completed threshold=%s found=%s", threshold, len(trending))
    return trending


async def generate_followup_angles(page_id: str) -> list[dict[str, Any]]:
    """Generate 3 Angle 2 ideas from a page headline and summary via Ollama."""

    if not page_id.strip():
        raise ValueError("page_id is required")

    settings = get_settings()
    notion = NotionClient(settings=settings)

    logger.info("angle_generation_started page_id=%s", page_id)
    page = await notion.get_page_model(page_id)
    blocks = await notion.list_block_children(page_id)
    summary = _extract_plain_text(blocks)[:2000]

    try:
        angles = await _generate_angles(settings=settings, headline=page.title, summary=summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("angle_generation_fallback page_id=%s error=%s", page_id, exc)
        angles = []

    if not angles:
        angles = _fallback_angles(headline=page.title, summary=summary)

    # Guarantee exactly 3 outputs.
    guaranteed = (angles + _fallback_angles(headline=page.title, summary=summary))[:3]
    logger.info("angle_generation_completed page_id=%s count=%s", page_id, len(guaranteed))
    return guaranteed


def _find_first_property_name(schema: dict[str, Any], property_type: str, hints: tuple[str, ...]) -> str | None:
    names = [name for name, data in schema.items() if isinstance(data, dict) and data.get("type") == property_type]
    for hint in hints:
        for name in names:
            if hint in name.lower():
                return name
    return names[0] if names else None


def _build_pitch_properties(
    schema: dict[str, Any],
    angle: dict[str, Any],
    original_page_id: str,
    created_at: str,
) -> dict[str, Any]:
    properties: dict[str, Any] = {}

    title_prop = _find_first_property_name(schema, "title", ("name", "title"))
    if title_prop:
        properties[title_prop] = {"title": [{"type": "text", "text": {"content": angle["title"]}}]}

    status_prop = _find_first_property_name(schema, "status", ("status",))
    if status_prop:
        properties[status_prop] = {"status": {"name": "Pitch"}}

    select_prop = _find_first_property_name(schema, "select", ("priority",))
    if select_prop:
        properties[select_prop] = {"select": {"name": angle.get("priority", "medium").capitalize()}}

    rich_text_prop = _find_first_property_name(schema, "rich_text", ("rationale", "summary", "angle", "note"))
    if rich_text_prop:
        properties[rich_text_prop] = {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": f"Hypothesis: {angle.get('hypothesis', '')}\n\nRationale: {angle.get('rationale', '')}",
                    },
                }
            ]
        }

    date_prop = _find_first_property_name(schema, "date", ("date", "created"))
    if date_prop:
        properties[date_prop] = {"date": {"start": created_at}}

    relation_prop = _find_first_property_name(schema, "relation", ("source", "original", "article"))
    if relation_prop:
        properties[relation_prop] = {"relation": [{"id": original_page_id}]}

    return properties


def _build_pitch_children(original_page_id: str, angle: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "text": {"content": "Backlink to original story: "}},
                    {
                        "type": "mention",
                        "mention": {"type": "page", "page": {"id": original_page_id}},
                    },
                ]
            },
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": f"Hypothesis: {angle.get('hypothesis', '')}",
                        },
                    }
                ]
            },
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": f"Rationale: {angle.get('rationale', '')}",
                        },
                    }
                ]
            },
        },
    ]


async def create_pitch_page(original_page_id: str, angle: dict[str, Any]) -> dict[str, Any]:
    """Create a pitch page in the configured Pitches database with backlink metadata."""

    if not original_page_id.strip():
        raise ValueError("original_page_id is required")

    normalized = _normalize_angle(angle, fallback_index=1)
    settings = get_settings()
    if not settings.notion.pitches_database_id:
        raise RuntimeError("NOTION_PITCHES_DATABASE_ID is required to create pitch pages")

    notion = NotionClient(settings=settings)
    logger.info("create_pitch_page_started original_page_id=%s", original_page_id)

    database = await notion.get_database(settings.notion.pitches_database_id)
    schema = database.get("properties", {}) if isinstance(database, dict) else {}
    schema = schema if isinstance(schema, dict) else {}

    created_at = datetime.now(UTC).date().isoformat()
    properties = _build_pitch_properties(
        schema=schema,
        angle=normalized,
        original_page_id=original_page_id,
        created_at=created_at,
    )
    children = _build_pitch_children(original_page_id=original_page_id, angle=normalized)

    page = await notion.create_page(
        parent={"database_id": settings.notion.pitches_database_id},
        properties=properties,
        children=children,
    )
    logger.info("create_pitch_page_completed original_page_id=%s pitch_page_id=%s", original_page_id, page.get("id"))
    return page
