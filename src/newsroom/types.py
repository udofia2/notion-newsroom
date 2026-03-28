"""Shared types for Notion MCP Newsroom OS.

This module contains two layers of typing:
1. ``TypedDict`` payload shapes for external API data and lightweight dictionaries.
2. ``Pydantic`` models for validated internal domain objects and MCP tool I/O.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


WorkflowName = Literal[
    "context_hunter",
    "traffic_strategist",
    "narrative_auditor",
    "agency_bridge",
]
AnalyticsProvider = Literal["ga4", "plausible"]
PitchPriority = Literal["low", "medium", "high", "urgent"]
AuditStatus = Literal["pass", "needs_revision", "fail"]
TrafficTrend = Literal["up", "flat", "down"]


class NewsroomError(Exception):
    """Base exception for Newsroom OS domain and workflow errors."""


class ConfigurationError(NewsroomError):
    """Raised when configuration is invalid or incomplete."""


class NotionClientError(NewsroomError):
    """Raised when Notion API operations fail."""


class ChromaSyncError(NewsroomError):
    """Raised when Chroma indexing or retrieval operations fail."""


class ContextHunterError(NewsroomError):
    """Raised by Context Hunter workflow failures."""


class TrafficStrategistError(NewsroomError):
    """Raised by Traffic Strategist workflow failures."""


class AuditError(NewsroomError):
    """Raised by Narrative Auditor workflow failures."""


class AgencyBridgeError(NewsroomError):
    """Raised by Agency Bridge workflow failures."""


class SchedulerError(NewsroomError):
    """Raised for background polling and scheduler dispatch failures."""


class NotionPropertyPayload(TypedDict, total=False):
    """Flexible representation of a Notion page property value."""

    id: str
    type: str
    name: str
    value: Any


class NotionPagePayload(TypedDict, total=False):
    """Minimal raw Notion page payload shape used by client adapters."""

    id: str
    object: str
    url: str
    created_time: str
    last_edited_time: str
    archived: bool
    properties: dict[str, NotionPropertyPayload]


class ChromaMatchPayload(TypedDict):
    """Raw Chroma query match payload before domain-model coercion."""

    id: str
    score: float
    document: str
    metadata: dict[str, Any]


class MCPResultPayload(TypedDict, total=False):
    """Generic MCP response envelope for dictionary-based handlers."""

    ok: bool
    message: str
    data: dict[str, Any]


class NotionPage(BaseModel):
    """Validated domain representation of a Notion page."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "id": "9b8f9f8d-3e30-4f62-9d97-c5f3f09c1f2e",
                "title": "Nigeria fintech funding trends",
                "url": "https://www.notion.so/9b8f9f8d3e304f629d97c5f3f09c1f2e",
                "status": "Draft",
                "tags": ["fintech", "africa"],
                "summary": "Analysis of 2025 Q4 capital allocation shifts.",
            }
        },
    )

    id: str = Field(min_length=8)
    title: str = Field(min_length=1)
    url: HttpUrl | None = None
    status: str | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    author: str | None = None
    created_time: datetime | None = None
    last_edited_time: datetime | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class HistoricalContext(BaseModel):
    """A single historical context record retrieved from vector search."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "source_page_id": "d8b45b9e-89d7-46ef-9010-a0af9f0179d1",
                "title": "How stablecoins won remittance volume",
                "snippet": "Remittance corridors with highest growth were...",
                "score": 0.87,
                "url": "https://www.notion.so/d8b45b9e89d746ef9010a0af9f0179d1",
                "tags": ["crypto", "remittance"],
            }
        },
    )

    source_page_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    snippet: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)
    url: HttpUrl | None = None
    tags: list[str] = Field(default_factory=list)
    published_at: datetime | None = None


class HistoricalContextResult(BaseModel):
    """Result object returned by context search workflows/tools."""

    model_config = ConfigDict(extra="forbid")

    page_id: str = Field(min_length=8)
    query: str = Field(min_length=2)
    contexts: list[HistoricalContext] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class TrafficSignal(BaseModel):
    """A normalized traffic signal used to inform strategy decisions."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "provider": "ga4",
                "metric": "page_views",
                "current_value": 5821,
                "previous_value": 4410,
                "change_pct": 32.0,
                "trend": "up",
                "window_minutes": 60,
                "top_referrers": ["google", "x.com", "newsletter"],
            }
        },
    )

    provider: AnalyticsProvider
    metric: str = Field(default="page_views", min_length=1)
    current_value: float = Field(ge=0)
    previous_value: float = Field(ge=0)
    change_pct: float
    trend: TrafficTrend
    window_minutes: int = Field(default=60, ge=1, le=1440)
    top_referrers: list[str] = Field(default_factory=list)
    observed_at: datetime = Field(default_factory=datetime.utcnow)


class PitchIdea(BaseModel):
    """A strategic content pitch generated from context and traffic signals."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "title": "Second angle: fintech layoffs are shifting to compliance hiring",
                "hypothesis": "Layoffs mask role redistribution into risk/compliance teams.",
                "rationale": "Traffic spikes on layoffs coverage + archive trend overlap.",
                "priority": "high",
                "confidence": 0.78,
                "supporting_signals": [
                    "+32% views in the last 60 minutes",
                    "3 related historical pieces with high similarity",
                ],
            }
        },
    )

    title: str = Field(min_length=5)
    hypothesis: str = Field(min_length=10)
    rationale: str = Field(min_length=10)
    priority: PitchPriority = "medium"
    confidence: float = Field(ge=0.0, le=1.0)
    target_audience: str | None = None
    recommended_format: str | None = None
    supporting_signals: list[str] = Field(default_factory=list)
    source_page_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AuditIssue(BaseModel):
    """An individual quality issue found during narrative audit."""

    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "clarity",
        "accuracy",
        "tone",
        "brand_alignment",
        "structure",
        "compliance",
    ]
    severity: Literal["low", "medium", "high"]
    message: str = Field(min_length=5)
    suggested_fix: str | None = None


class AuditResult(BaseModel):
    """Structured output from narrative auditor workflow."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "status": "needs_revision",
                "score": 74,
                "summary": "Strong structure, but voice drifts from brand guidance.",
                "issues": [
                    {
                        "category": "brand_alignment",
                        "severity": "high",
                        "message": "Opening paragraph overuses hype framing.",
                        "suggested_fix": "Shift to evidence-first lead with one statistic.",
                    }
                ],
                "recommendations": [
                    "Replace generic claims with sourced statements.",
                    "Shorten paragraph 3 by 30%.",
                ],
            }
        },
    )

    status: AuditStatus
    score: int = Field(ge=0, le=100)
    summary: str = Field(min_length=10)
    issues: list[AuditIssue] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    draft_page_id: str | None = None
    brand_guide_page_id: str | None = None
    checked_at: datetime = Field(default_factory=datetime.utcnow)
    tool_version: str = "v1"


class AgencyBridgePayload(BaseModel):
    """Outbound payload sent to external agency endpoints."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3)
    slug: str = Field(min_length=3)
    markdown: str = Field(min_length=1)
    html: str = Field(min_length=1)
    source_page_id: str = Field(min_length=8)
    tags: list[str] = Field(default_factory=list)


class WorkflowRun(BaseModel):
    """Metadata about a workflow execution for logs and status tracking."""

    model_config = ConfigDict(extra="forbid")

    workflow: WorkflowName
    status: Literal["queued", "running", "completed", "failed"]
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    page_id: str | None = None
    detail: str | None = None


__all__ = [
    "AgencyBridgeError",
    "AgencyBridgePayload",
    "AnalyticsProvider",
    "AuditError",
    "AuditIssue",
    "AuditResult",
    "AuditStatus",
    "ChromaSyncError",
    "ChromaMatchPayload",
    "ConfigurationError",
    "ContextHunterError",
    "HistoricalContext",
    "HistoricalContextResult",
    "MCPResultPayload",
    "NewsroomError",
    "NotionClientError",
    "NotionPage",
    "NotionPagePayload",
    "NotionPropertyPayload",
    "PitchIdea",
    "PitchPriority",
    "SchedulerError",
    "TrafficStrategistError",
    "TrafficSignal",
    "TrafficTrend",
    "WorkflowName",
    "WorkflowRun",
]
