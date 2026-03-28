"""Project constants and shared logging helpers."""

from __future__ import annotations

import logging
from typing import Any

# ---------------------------------------------------------------------------
# Notion fixed IDs (override with environment-specific config when required)
# ---------------------------------------------------------------------------
NOTION_BRAND_GUIDE_PAGE_ID = "ethics-brand-guide-fixed-page-id"
NOTION_PITCH_DATABASE_ID = "pitch-database-fixed-id"
NOTION_STORY_DATABASE_ID = "story-database-fixed-id"
NOTION_DASHBOARD_PAGE_ID = "newsroom-dashboard-fixed-page-id"
NOTION_ARCHIVE_DATABASE_ID = "story-archive-database-fixed-id"

# ---------------------------------------------------------------------------
# Workflow statuses
# ---------------------------------------------------------------------------
STATUS_RESEARCHING = "Researching"
STATUS_NEEDS_AUDIT = "Needs Audit"
STATUS_APPROVED_FOR_PUBLICATION = "Approved for Publication"
STATUS_PUBLISHED = "Published"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
DEFAULT_LOG_FORMAT = (
    "%(asctime)s level=%(levelname)s logger=%(name)s "
    "message=%(message)s context=%(context)s"
)


class _ContextFieldFilter(logging.Filter):
    """Ensure every log record has a `context` field for format safety."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "context"):
            record.context = ""
        return True


class ContextLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that appends deterministic key=value context fields."""

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        extra = kwargs.setdefault("extra", {})
        runtime_ctx = extra.get("context")

        merged: dict[str, Any] = {}
        for source in (self.extra, runtime_ctx if isinstance(runtime_ctx, dict) else {}):
            for key, value in source.items():
                merged[key] = value

        context_str = " ".join(f"{key}={value}" for key, value in sorted(merged.items()))
        extra["context"] = context_str
        return msg, kwargs


def setup_structured_logging(level: str = "INFO") -> None:
    """Initialize a stable project-wide structured log format."""

    root_logger = logging.getLogger()
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(resolved_level)

    context_filter = _ContextFieldFilter()
    formatter = logging.Formatter(DEFAULT_LOG_FORMAT)

    if not root_logger.handlers:
        root_logger.addHandler(logging.StreamHandler())

    for handler in root_logger.handlers:
        handler.setLevel(resolved_level)
        handler.addFilter(context_filter)
        handler.setFormatter(formatter)


def get_logger(name: str, **context: Any) -> ContextLoggerAdapter:
    """Return context-aware logger adapter for structured key=value logging."""

    return ContextLoggerAdapter(logging.getLogger(name), context)


__all__ = [
    "ContextLoggerAdapter",
    "DEFAULT_LOG_FORMAT",
    "NOTION_ARCHIVE_DATABASE_ID",
    "NOTION_BRAND_GUIDE_PAGE_ID",
    "NOTION_DASHBOARD_PAGE_ID",
    "NOTION_PITCH_DATABASE_ID",
    "NOTION_STORY_DATABASE_ID",
    "STATUS_APPROVED_FOR_PUBLICATION",
    "STATUS_NEEDS_AUDIT",
    "STATUS_PUBLISHED",
    "STATUS_RESEARCHING",
    "get_logger",
    "setup_structured_logging",
]
