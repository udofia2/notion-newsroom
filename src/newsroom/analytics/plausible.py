"""Plausible analytics fallback with GA-like normalized output."""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

from newsroom.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _seed_from_page(page_id: str) -> int:
    digest = hashlib.md5(page_id.encode("utf-8")).hexdigest()  # noqa: S324
    return int(digest[:8], 16)


def _emoji_for_traffic(recent_views: int, baseline_views: int) -> str:
    baseline = max(1, baseline_views)
    ratio = recent_views / baseline
    if recent_views >= 250 and ratio >= 1.6:
        return "🔥"
    return "🧊"


def _build_referral_breakdown(rng: random.Random) -> list[dict[str, Any]]:
    channels = ["google", "direct", "x.com", "newsletter", "linkedin"]
    raw = [rng.randint(4, 36) for _ in channels]
    total = max(1, sum(raw))
    breakdown: list[dict[str, Any]] = []
    for source, views in zip(channels, raw):
        breakdown.append(
            {
                "source": source,
                "views": views,
                "share_pct": round((views / total) * 100, 2),
            }
        )
    breakdown.sort(key=lambda item: int(item["views"]), reverse=True)
    return breakdown


async def _plausible_api_stub(page_id: str, settings: Settings) -> dict[str, Any]:
    """Deterministic fake Plausible response for offline development."""

    del settings
    rng = random.Random(_seed_from_page(page_id))
    baseline_60m = rng.randint(80, 300)
    recent_60m = max(1, baseline_60m + rng.randint(-60, 350))
    views_24h = recent_60m * rng.randint(9, 20)

    return {
        "site_id": "plausible-stub-site",
        "page_id": page_id,
        "metrics": {
            "views_60m": recent_60m,
            "baseline_views_60m": baseline_60m,
            "views_24h": views_24h,
        },
        "referrals": _build_referral_breakdown(rng),
        "window": {
            "recent_minutes": 60,
            "baseline_days": 7,
        },
    }


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    metrics = raw.get("metrics", {}) if isinstance(raw, dict) else {}
    referrals = raw.get("referrals", []) if isinstance(raw, dict) else []

    recent_60m = int(metrics.get("views_60m", 0) or 0)
    baseline_60m = int(metrics.get("baseline_views_60m", 0) or 0)
    views_24h = int(metrics.get("views_24h", 0) or 0)

    baseline = max(1, baseline_60m)
    ratio = round(recent_60m / baseline, 3)
    pct_change = round(((recent_60m - baseline_60m) / baseline) * 100, 2)
    emoji = _emoji_for_traffic(recent_60m, baseline_60m)

    return {
        "provider": "plausible",
        "page_id": str(raw.get("page_id") or ""),
        "view_count": views_24h,
        "recent_spike": {
            "is_spiking": emoji == "🔥",
            "emoji": emoji,
            "views_last_60m": recent_60m,
            "baseline_views_60m": baseline_60m,
            "delta": recent_60m - baseline_60m,
            "spike_ratio": ratio,
            "pct_change": pct_change,
        },
        "referral_breakdown": referrals if isinstance(referrals, list) else [],
    }


async def get_page_traffic(page_id: str, settings: Settings | None = None) -> dict[str, Any]:
    """Return normalized traffic for a page using Plausible-compatible shape."""

    if not page_id.strip():
        raise ValueError("page_id is required")

    resolved = settings or get_settings()
    raw = await _plausible_api_stub(page_id=page_id, settings=resolved)
    normalized = _normalize(raw)
    logger.info(
        "plausible_page_traffic page_id=%s view_count=%s emoji=%s",
        page_id,
        normalized["view_count"],
        normalized["recent_spike"]["emoji"],
    )
    return normalized


async def fetch_realtime_story_views(
    settings: Settings | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return synthetic Plausible rows for story-level trending checks."""

    resolved = settings or get_settings()
    rows: list[dict[str, Any]] = []
    bounded = max(1, min(limit, 100))

    for idx in range(bounded):
        page_id = f"stub-plausible-page-{idx + 1}"
        traffic = await get_page_traffic(page_id=page_id, settings=resolved)
        rows.append(
            {
                "page_id": page_id,
                "title": f"Plausible Stub Story {idx + 1}",
                "summary": "Synthetic plausible analytics record used for development.",
                "views": int(traffic["view_count"]),
                "previous_views": int(traffic["recent_spike"]["baseline_views_60m"]) * 14,
                "url": None,
            }
        )

    return rows


__all__ = ["fetch_realtime_story_views", "get_page_traffic"]
