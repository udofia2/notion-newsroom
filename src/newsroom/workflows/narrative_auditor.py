"""Narrative Auditor workflow.

Evaluates draft quality against an ethics and brand guide, then posts constructive
sentence-level feedback to the Notion page.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from newsroom.config import Settings, get_settings
from newsroom.llm import generate_text
from newsroom.notion.blocks import post_sentence_level_audit_comments
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


async def _read_page_text_recursive(
    notion: NotionClient,
    block_id: str,
    depth: int = 0,
    max_depth: int = 5,
) -> str:
    blocks = await notion.list_block_children(block_id)
    local_text = _extract_plain_text(blocks)
    if depth >= max_depth:
        return local_text

    nested_parts: list[str] = [local_text] if local_text else []
    for block in blocks:
        child_id = block.get("id")
        has_children = bool(block.get("has_children"))
        if has_children and isinstance(child_id, str):
            child_text = await _read_page_text_recursive(
                notion=notion,
                block_id=child_id,
                depth=depth + 1,
                max_depth=max_depth,
            )
            if child_text:
                nested_parts.append(child_text)
    return "\n\n".join(part for part in nested_parts if part).strip()


def _audit_system_prompt() -> str:
    return (
        "You are a senior newsroom standards editor. Evaluate a draft for:\n"
        "1) bias and fairness\n"
        "2) tone and brand voice\n"
        "3) citation and evidence quality\n"
        "4) clarity and structure\n\n"
        "Output strict JSON only with this schema:\n"
        "{\"summary\":\"...\",\"overall_status\":\"pass|needs_revision|fail\","
        "\"score\":0," 
        "\"findings\":[{\"sentence\":\"...\",\"issue\":\"...\","
        "\"category\":\"bias|tone|citation|brand_voice|clarity\","
        "\"severity\":\"low|medium|high\",\"suggestion\":\"...\"}],"
        "\"recommendations\":[\"...\"]}\n"
        "Use a constructive and professional tone. Keep findings actionable."
    )


def _audit_user_prompt(draft_text: str, guide_text: str) -> str:
    return (
        "Ethics and Brand Guide:\n"
        f"{guide_text[:3000]}\n\n"
        "Draft to audit:\n"
        f"{draft_text[:6000]}\n"
    )


def _fallback_audit(draft_text: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", draft_text) if s.strip()]

    for sentence in sentences[:8]:
        if len(sentence.split()) > 35:
            findings.append(
                {
                    "sentence": sentence,
                    "issue": "Sentence is too long and may reduce readability.",
                    "category": "clarity",
                    "severity": "medium",
                    "suggestion": "Split this sentence into two shorter, evidence-led statements.",
                }
            )
        if re.search(r"\b(always|never|everyone|no one)\b", sentence, re.I):
            findings.append(
                {
                    "sentence": sentence,
                    "issue": "Contains absolute phrasing that may overstate certainty.",
                    "category": "bias",
                    "severity": "medium",
                    "suggestion": "Use qualified language and provide source-backed nuance.",
                }
            )

    return {
        "summary": "Fallback audit used due to model unavailability/timeout. Manual review is recommended.",
        "overall_status": "needs_revision",
        "score": 55 if findings else 60,
        "findings": findings[:6],
        "recommendations": [
            "Add explicit sourcing for key claims.",
            "Ensure tone remains neutral and evidence-first.",
            "Rerun narrative audit once model responsiveness is restored.",
        ],
    }


async def _run_model_audit(
    settings: Settings,
    draft_text: str,
    guide_text: str,
) -> dict[str, Any]:
    timeout_seconds = min(60, settings.request_timeout_seconds)
    prompt = _audit_user_prompt(draft_text=draft_text, guide_text=guide_text)
    system = _audit_system_prompt()
    try:
        raw = await generate_text(
            settings=settings,
            prompt=prompt,
            system=system,
            timeout_seconds=timeout_seconds,
        )
    except httpx.ReadTimeout:
        logger.warning(
            "narrative_audit_model_timeout timeout_seconds=%s",
            timeout_seconds,
        )
        raise
    if not raw:
        raise ValueError("Model returned empty response")

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    parsed = json.loads(match.group(0) if match else raw)
    if not isinstance(parsed, dict):
        raise ValueError("Ollama returned non-dict audit payload")
    return parsed


def _normalize_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary = str(payload.get("summary") or "Narrative audit completed.").strip()
    status = str(payload.get("overall_status") or "needs_revision").strip().lower()
    if status not in {"pass", "needs_revision", "fail"}:
        status = "needs_revision"

    raw_findings = payload.get("findings", [])
    findings: list[dict[str, Any]] = []
    if isinstance(raw_findings, list):
        for item in raw_findings[:8]:
            if not isinstance(item, dict):
                continue
            findings.append(
                {
                    "sentence": str(item.get("sentence") or "").strip(),
                    "issue": str(item.get("issue") or "Needs revision.").strip(),
                    "category": str(item.get("category") or "clarity").strip().lower(),
                    "severity": str(item.get("severity") or "medium").strip().lower(),
                    "suggestion": str(item.get("suggestion") or "Revise for evidence and clarity.").strip(),
                }
            )

    recommendations = payload.get("recommendations", [])
    normalized_recommendations: list[str] = []
    if isinstance(recommendations, list):
        normalized_recommendations = [str(item).strip() for item in recommendations if str(item).strip()]

    score_raw = payload.get("score", 70)
    try:
        score = int(score_raw)
    except (TypeError, ValueError):
        score = 70
    score = max(0, min(100, score))

    return {
        "summary": summary,
        "overall_status": status,
        "score": score,
        "findings": findings,
        "recommendations": normalized_recommendations,
    }


async def run_narrative_audit(page_id: str) -> dict[str, Any]:
    """Run narrative audit and post constructive sentence-level comments to Notion."""

    if not page_id.strip():
        raise ValueError("page_id is required")

    settings = get_settings()
    notion = NotionClient(settings=settings)
    brand_guide_page_id = settings.notion.brand_guide_page_id

    logger.info("narrative_audit_started page_id=%s", page_id)

    draft_text = await _read_page_text_recursive(notion=notion, block_id=page_id)
    if not draft_text:
        logger.info("narrative_audit_no_content page_id=%s", page_id)
        return {
            "ok": True,
            "status": "no_content",
            "page_id": page_id,
            "comments_posted": 0,
        }

    guide_text = await _read_page_text_recursive(notion=notion, block_id=brand_guide_page_id)
    if not guide_text:
        raise RuntimeError("Ethics & Brand Guide page returned no readable text")

    try:
        raw_audit = await _run_model_audit(settings=settings, draft_text=draft_text, guide_text=guide_text)
        fallback_used = False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "narrative_audit_fallback page_id=%s error_type=%s error=%r",
            page_id,
            type(exc).__name__,
            exc,
        )
        raw_audit = _fallback_audit(draft_text=draft_text)
        fallback_used = True

    audit = _normalize_audit_payload(raw_audit)
    comments = await post_sentence_level_audit_comments(
        notion=notion,
        page_id=page_id,
        summary=audit["summary"],
        findings=audit["findings"],
    )

    logger.info(
        "narrative_audit_completed page_id=%s status=%s score=%s findings=%s comments=%s fallback_used=%s",
        page_id,
        audit["overall_status"],
        audit["score"],
        len(audit["findings"]),
        len(comments),
        fallback_used,
    )
    return {
        "ok": True,
        "page_id": page_id,
        "brand_guide_page_id": brand_guide_page_id,
        "reliable": not fallback_used,
        "audit": {
            **audit,
            "checked_at": datetime.now(UTC).isoformat(),
            "fallback_used": fallback_used,
        },
        "comments_posted": len(comments),
    }
