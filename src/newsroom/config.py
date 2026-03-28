"""Application configuration built on Pydantic v2 settings."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, PositiveInt, field_validator
from dotenv import dotenv_values
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from newsroom.constants import (
    NOTION_BRAND_GUIDE_PAGE_ID,
    NOTION_DASHBOARD_PAGE_ID,
    NOTION_PITCH_DATABASE_ID,
    NOTION_STORY_DATABASE_ID,
)


class NotionSettings(BaseModel):
    """Settings for Notion API access and newsroom entities."""

    token: str = Field(default="secret_placeholder_token", min_length=10, validation_alias="NOTION_TOKEN")
    database_id: str = Field(
        default=NOTION_STORY_DATABASE_ID,
        min_length=8,
        validation_alias="NOTION_DATABASE_ID",
    )
    dashboard_page_id: str = Field(
        default=NOTION_DASHBOARD_PAGE_ID,
        min_length=8,
        validation_alias="NOTION_DASHBOARD_PAGE_ID",
    )
    brand_guide_page_id: str = Field(
        default=NOTION_BRAND_GUIDE_PAGE_ID,
        min_length=8,
        validation_alias="NOTION_BRAND_GUIDE_PAGE_ID",
    )
    pitches_database_id: str | None = Field(
        default=NOTION_PITCH_DATABASE_ID,
        validation_alias="NOTION_PITCHES_DATABASE_ID",
    )
    articles_database_id: str | None = Field(
        default=None,
        validation_alias="NOTION_ARTICLES_DATABASE_ID",
    )


class OllamaSettings(BaseModel):
    """Settings for model generation and embedding with Ollama."""

    host: HttpUrl = Field(default="http://localhost:11434", validation_alias="OLLAMA_HOST")
    generation_model: str = Field(default="llama3.2:3b", validation_alias="OLLAMA_GENERATION_MODEL")
    embedding_model: str = Field(
        default="nomic-embed-text:v1.5",
        validation_alias="OLLAMA_EMBEDDING_MODEL",
    )


class GeminiSettings(BaseModel):
    """Settings for Gemini generation model access."""

    api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    model: str = Field(default="gemini-2.0-flash", validation_alias="GEMINI_MODEL")
    base_url: HttpUrl = Field(
        default="https://generativelanguage.googleapis.com",
        validation_alias="GEMINI_BASE_URL",
    )


class ChromaSettings(BaseModel):
    """Settings for Chroma persistence and retrieval behavior."""

    persist_directory: str = Field(default="./.chroma", validation_alias="CHROMA_PERSIST_DIRECTORY")
    collection_name: str = Field(default="notion_newsroom_archive", validation_alias="CHROMA_COLLECTION_NAME")
    top_k: PositiveInt = Field(default=8, le=50, validation_alias="CHROMA_TOP_K")
    relevance_threshold: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        validation_alias="CHROMA_RELEVANCE_THRESHOLD",
    )


class AnalyticsSettings(BaseModel):
    """Settings for analytics providers and credentials."""

    provider: Literal["ga4", "plausible"] = Field(default="ga4", validation_alias="ANALYTICS_PROVIDER")
    ga4_property_id: str | None = Field(default=None, validation_alias="GA4_PROPERTY_ID")
    google_application_credentials: str | None = Field(
        default=None,
        validation_alias="GOOGLE_APPLICATION_CREDENTIALS",
    )
    plausible_base_url: HttpUrl = Field(
        default="https://plausible.io/api/v1",
        validation_alias="PLAUSIBLE_BASE_URL",
    )
    plausible_api_key: str | None = Field(default=None, validation_alias="PLAUSIBLE_API_KEY")
    plausible_site_id: str | None = Field(default=None, validation_alias="PLAUSIBLE_SITE_ID")


class SchedulerSettings(BaseModel):
    """Settings for APScheduler polling and workflow execution cadence."""

    enabled: bool = Field(default=True, validation_alias="SCHEDULER_ENABLED")
    poll_interval_seconds: PositiveInt = Field(
        default=120,
        ge=30,
        le=3600,
        validation_alias="SCHEDULER_POLL_INTERVAL_SECONDS",
    )
    workflow_worker_count: PositiveInt = Field(
        default=2,
        ge=1,
        le=16,
        validation_alias="SCHEDULER_WORKFLOW_WORKER_COUNT",
    )
    workflow_queue_size: PositiveInt = Field(
        default=1000,
        ge=100,
        le=20000,
        validation_alias="SCHEDULER_WORKFLOW_QUEUE_SIZE",
    )


class LoggingSettings(BaseModel):
    """Settings for application logging behavior."""

    level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"] = Field(
        default="INFO",
        validation_alias="LOG_LEVEL",
    )


class Settings(BaseSettings):
    """Top-level application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    app_env: Literal["development", "staging", "production", "test"] = Field(
        default="development",
        validation_alias="APP_ENV",
    )
    mcp_server_host: str = Field(default="0.0.0.0", validation_alias="MCP_SERVER_HOST")
    mcp_server_port: int = Field(default=8000, ge=1, le=65535, validation_alias="MCP_SERVER_PORT")
    request_timeout_seconds: PositiveInt = Field(
        default=30,
        ge=1,
        le=300,
        validation_alias="REQUEST_TIMEOUT_SECONDS",
    )
    enabled_workflows: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(
            "context_hunter",
            "traffic_strategist",
            "narrative_auditor",
            "agency_bridge",
        ),
        validation_alias="ENABLED_WORKFLOWS",
    )

    notion: NotionSettings = Field(default_factory=NotionSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    chroma: ChromaSettings = Field(default_factory=ChromaSettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @field_validator("enabled_workflows", mode="before")
    @classmethod
    def _parse_enabled_workflows(cls, value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(value, tuple):
            return value
        if isinstance(value, list):
            cleaned = tuple(part.strip() for part in value if part.strip())
            if cleaned:
                return cleaned
            raise ValueError("ENABLED_WORKFLOWS cannot be empty")
        if isinstance(value, str):
            cleaned = tuple(part.strip() for part in value.split(",") if part.strip())
            if cleaned:
                return cleaned
            raise ValueError("ENABLED_WORKFLOWS cannot be empty")
        raise TypeError("ENABLED_WORKFLOWS must be a comma-separated string or sequence")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached, process-wide settings instance."""

    env_path = Path(__file__).resolve().parents[2] / ".env"
    dotenv_map = dotenv_values(env_path) if env_path.exists() else {}

    def _env_value(name: str) -> str | None:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
        dot_val = dotenv_map.get(name)
        if isinstance(dot_val, str) and dot_val != "":
            return dot_val
        return None

    top_level_overrides: dict[str, str] = {}
    top_level_mapping = (
        "APP_ENV",
        "MCP_SERVER_HOST",
        "MCP_SERVER_PORT",
        "REQUEST_TIMEOUT_SECONDS",
        "ENABLED_WORKFLOWS",
    )
    for env_key in top_level_mapping:
        value = _env_value(env_key)
        if value is not None:
            top_level_overrides[env_key] = value

    notion_overrides: dict[str, str] = {}
    notion_mapping = (
        "NOTION_TOKEN",
        "NOTION_DATABASE_ID",
        "NOTION_DASHBOARD_PAGE_ID",
        "NOTION_BRAND_GUIDE_PAGE_ID",
        "NOTION_PITCHES_DATABASE_ID",
        "NOTION_ARTICLES_DATABASE_ID",
    )
    for env_key in notion_mapping:
        value = _env_value(env_key)
        if value is not None:
            notion_overrides[env_key] = value

    ollama_overrides: dict[str, str] = {}
    ollama_mapping = (
        "OLLAMA_HOST",
        "OLLAMA_GENERATION_MODEL",
        "OLLAMA_EMBEDDING_MODEL",
    )
    for env_key in ollama_mapping:
        value = _env_value(env_key)
        if value is not None:
            ollama_overrides[env_key] = value

    gemini_overrides: dict[str, str] = {}
    gemini_mapping = (
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GEMINI_BASE_URL",
    )
    for env_key in gemini_mapping:
        value = _env_value(env_key)
        if value is not None:
            gemini_overrides[env_key] = value

    chroma_overrides: dict[str, str] = {}
    chroma_mapping = (
        "CHROMA_PERSIST_DIRECTORY",
        "CHROMA_COLLECTION_NAME",
        "CHROMA_TOP_K",
        "CHROMA_RELEVANCE_THRESHOLD",
    )
    for env_key in chroma_mapping:
        value = _env_value(env_key)
        if value is not None:
            chroma_overrides[env_key] = value

    analytics_overrides: dict[str, str] = {}
    analytics_mapping = (
        "ANALYTICS_PROVIDER",
        "GA4_PROPERTY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "PLAUSIBLE_BASE_URL",
        "PLAUSIBLE_API_KEY",
        "PLAUSIBLE_SITE_ID",
    )
    for env_key in analytics_mapping:
        value = _env_value(env_key)
        if value is not None:
            analytics_overrides[env_key] = value

    scheduler_overrides: dict[str, str] = {}
    scheduler_mapping = (
        "SCHEDULER_ENABLED",
        "SCHEDULER_POLL_INTERVAL_SECONDS",
        "SCHEDULER_WORKFLOW_WORKER_COUNT",
        "SCHEDULER_WORKFLOW_QUEUE_SIZE",
    )
    for env_key in scheduler_mapping:
        value = _env_value(env_key)
        if value is not None:
            scheduler_overrides[env_key] = value

    logging_overrides: dict[str, str] = {}
    log_level = _env_value("LOG_LEVEL")
    if log_level is not None:
        logging_overrides["LOG_LEVEL"] = log_level

    settings_kwargs: dict[str, object] = dict(top_level_overrides)
    if notion_overrides:
        settings_kwargs["notion"] = notion_overrides
    if ollama_overrides:
        settings_kwargs["ollama"] = ollama_overrides
    if gemini_overrides:
        settings_kwargs["gemini"] = gemini_overrides
    if chroma_overrides:
        settings_kwargs["chroma"] = chroma_overrides
    if analytics_overrides:
        settings_kwargs["analytics"] = analytics_overrides
    if scheduler_overrides:
        settings_kwargs["scheduler"] = scheduler_overrides
    if logging_overrides:
        settings_kwargs["logging"] = logging_overrides

    return Settings(**settings_kwargs)
