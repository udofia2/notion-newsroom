"""Google Analytics traffic module with GA4 stub and Plausible fallback."""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any

from newsroom.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _seed_from_page(page_id: str) -> int:
    digest = hashlib.sha256(page_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _emoji_for_traffic(recent_views: int, baseline_views: int) -> str:
    baseline = max(1, baseline_views)
    spike_ratio = recent_views / baseline
    if recent_views >= 300 and spike_ratio >= 1.8:
        return "🔥"
    return "🧊"


def _build_referral_breakdown(rng: random.Random) -> list[dict[str, Any]]:
    channels = ["google", "x.com", "newsletter", "direct", "linkedin", "reddit"]
    raw_values = [rng.randint(5, 40) for _ in channels]
    total = max(1, sum(raw_values))

    breakdown: list[dict[str, Any]] = []
    for source, value in zip(channels, raw_values):
        percent = round((value / total) * 100, 2)
        breakdown.append({"source": source, "views": value, "share_pct": percent})
    breakdown.sort(key=lambda item: int(item["views"]), reverse=True)
    return breakdown


async def _ga4_data_api_stub(page_id: str, settings: Settings) -> dict[str, Any]:
    """Return deterministic fake GA4-like response shape for development."""

    del settings
    rng = random.Random(_seed_from_page(page_id))
    baseline_60m = rng.randint(120, 420)
    recent_60m = max(1, baseline_60m + rng.randint(-80, 420))
    views_24h = recent_60m * rng.randint(10, 22)
    referrals = _build_referral_breakdown(rng)

    return {
        "property_id": "ga4-stub-property",
        "page_id": page_id,
        "metrics": {
            "views_60m": recent_60m,
            "baseline_views_60m": baseline_60m,
            "views_24h": views_24h,
        },
        "referrals": referrals,
        "window": {
            "recent_minutes": 60,
            "baseline_days": 7,
        },
    }


def _normalize_traffic(provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    metrics = raw.get("metrics", {}) if isinstance(raw, dict) else {}
    referrals = raw.get("referrals", []) if isinstance(raw, dict) else []

    recent_60m = int(metrics.get("views_60m", 0) or 0)
    baseline_60m = int(metrics.get("baseline_views_60m", 0) or 0)
    views_24h = int(metrics.get("views_24h", 0) or 0)

    delta = recent_60m - baseline_60m
    baseline = max(1, baseline_60m)
    ratio = round(recent_60m / baseline, 3)
    pct_change = round(((recent_60m - baseline_60m) / baseline) * 100, 2)
    emoji = _emoji_for_traffic(recent_60m, baseline_60m)

    return {
        "provider": provider,
        "page_id": str(raw.get("page_id") or ""),
        "view_count": views_24h,
        "recent_spike": {
            "is_spiking": emoji == "🔥",
            "emoji": emoji,
            "views_last_60m": recent_60m,
            "baseline_views_60m": baseline_60m,
            "delta": delta,
            "spike_ratio": ratio,
            "pct_change": pct_change,
        },
        "referral_breakdown": referrals if isinstance(referrals, list) else [],
    }


async def fetch_realtime_story_views(
    settings: Settings | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent per-story view snapshots.

    Stub shape returned per item:
    - page_id: str
    - title: str
    - summary: str
    - views: int
    - previous_views: int
    - url: str | None
    """

    resolved = settings or get_settings()
    logger.info(
        "ga_stub_fetch property_id=%s provider=%s limit=%s",
        resolved.analytics.ga4_property_id,
        resolved.analytics.provider,
        limit,
    )

    rows: list[dict[str, Any]] = []
    bounded = max(1, min(limit, 100))
    for idx in range(bounded):
        page_id = f"stub-ga4-page-{idx + 1}"
        raw = await _ga4_data_api_stub(page_id=page_id, settings=resolved)
        metrics = raw["metrics"]
        rows.append(
            {
                "page_id": page_id,
                "title": f"GA4 Stub Story {idx + 1}",
                "summary": "Synthetic analytics record used for local development workflows.",
                "views": int(metrics["views_24h"]),
                "previous_views": int(metrics["baseline_views_60m"]) * 16,
                "url": None,
            }
        )
    return rows


async def get_page_traffic(
    page_id: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return normalized traffic snapshot for one page.

    Response includes:
    - view_count
    - recent_spike metadata
    - referral_breakdown
    - emoji signal ("🔥" spiking, "🧊" evergreen)
    """

    if not page_id.strip():
        raise ValueError("page_id is required")

    resolved = settings or get_settings()
    provider = resolved.analytics.provider

    if provider == "plausible":
        from newsroom.analytics.plausible import get_page_traffic as plausible_get_page_traffic

        return await plausible_get_page_traffic(page_id=page_id, settings=resolved)

    try:
        raw = await _ga4_data_api_stub(page_id=page_id, settings=resolved)
        normalized = _normalize_traffic(provider="ga4", raw=raw)
        logger.info(
            "ga4_page_traffic page_id=%s view_count=%s emoji=%s",
            page_id,
            normalized["view_count"],
            normalized["recent_spike"]["emoji"],
        )
        return normalized
    except Exception as exc:  # noqa: BLE001
        logger.warning("ga4_failed_fallback_to_plausible page_id=%s error=%s", page_id, exc)
        from newsroom.analytics.plausible import get_page_traffic as plausible_get_page_traffic

        return await plausible_get_page_traffic(page_id=page_id, settings=resolved)


__all__ = ["fetch_realtime_story_views", "get_page_traffic"]
