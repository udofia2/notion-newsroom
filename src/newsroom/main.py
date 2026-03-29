"""Application entrypoint for Newsroom OS.

Bootstraps:
"""

from __future__ import annotations

import importlib
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Callable

from dotenv import load_dotenv

from newsroom.chroma.manager import ChromaManager
from newsroom.config import Settings, get_settings
from newsroom.constants import get_logger, setup_structured_logging
from newsroom.mcp.server import app as mcp_server
from newsroom.mcp.server import configure_dependencies
from newsroom.notion.client import NotionClient
from newsroom.scheduler.jobs import shutdown_scheduler, start_scheduler

# Load .env file before any settings are instantiated
load_dotenv(override=False)

setup_structured_logging()
logger = get_logger(__name__, component="main")

_fastapi_module = importlib.import_module("fastapi")
FastAPI = getattr(_fastapi_module, "FastAPI")


def _build_shared_clients(settings: Settings) -> tuple[NotionClient, ChromaManager]:
    notion = NotionClient(settings=settings)
    chroma = ChromaManager(
        persist_directory=settings.chroma.persist_directory,
        ollama_host=str(settings.ollama.host),
        embedding_model=settings.ollama.embedding_model,
        collection_name=settings.chroma.collection_name,
    )
    return notion, chroma


def _resolve_mcp_asgi_app(server: Any) -> Callable[..., Any] | None:
    """Best-effort extraction of an ASGI app from FastMCP server object."""

    direct_attrs = ("asgi_app", "app")
    for attr in direct_attrs:
        candidate = getattr(server, attr, None)
        if callable(candidate):
            return candidate

    methods = ("http_app", "asgi", "to_asgi", "create_app", "get_app")
    for method_name in methods:
        method = getattr(server, method_name, None)
        if callable(method):
            try:
                if method_name == "http_app":
                    candidate = method(path="/")
                else:
                    candidate = method()
            except TypeError:
                if method_name == "http_app":
                    try:
                        candidate = method()
                    except TypeError:
                        continue
                else:
                    continue
            if callable(candidate):
                return candidate

    if callable(server):
        return server
    return None


def _resolve_mcp_lifespan(asgi_app: Any) -> Callable[[Any], Any] | None:
    """Resolve lifespan hook from FastMCP Starlette app if available."""

    if asgi_app is None:
        return None

    lifespan = getattr(asgi_app, "lifespan", None)
    if callable(lifespan):
        return lifespan

    router = getattr(asgi_app, "router", None)
    if router is not None:
        lifespan_context = getattr(router, "lifespan_context", None)
        if callable(lifespan_context):
            return lifespan_context

    return None


@asynccontextmanager
async def _lifespan(app: Any):
    settings = get_settings()
    notion, chroma = _build_shared_clients(settings)

    app.state.settings = settings
    app.state.notion = notion
    app.state.chroma = chroma

    configure_dependencies(
        settings_provider=lambda: settings,
        notion_factory=lambda _settings: notion,
        chroma_factory=lambda _settings: chroma,
    )

    scheduler = None
    if settings.scheduler.enabled:
        scheduler = start_scheduler(interval_seconds=settings.scheduler.poll_interval_seconds)
    app.state.scheduler = scheduler

    logger.info(
        "newsroom_startup env=%s scheduler_enabled=%s scheduler_interval=%s",
        settings.app_env,
        settings.scheduler.enabled,
        settings.scheduler.poll_interval_seconds,
    )

    try:
        yield
    finally:
        shutdown_scheduler(wait=False)
        logger.info("newsroom_shutdown_complete")


_mcp_asgi_app = _resolve_mcp_asgi_app(mcp_server)
_mcp_lifespan = _resolve_mcp_lifespan(_mcp_asgi_app)


@asynccontextmanager
async def _app_lifespan(app: Any):
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(_lifespan(app))
        if _mcp_lifespan is not None:
            await stack.enter_async_context(_mcp_lifespan(app))
        yield


app = FastAPI(title="notion-newsroom", version="0.1.0", lifespan=_app_lifespan)

if _mcp_asgi_app is not None:
    app.mount("/mcp", _mcp_asgi_app)
    _mcp_mounted = True
else:
    _mcp_mounted = False


@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Basic health endpoint for runtime and orchestration checks."""

    settings = getattr(app.state, "settings", None)
    scheduler = getattr(app.state, "scheduler", None)
    scheduler_running = bool(getattr(scheduler, "running", False))

    return {
        "ok": True,
        "service": "notion-newsroom",
        "environment": getattr(settings, "app_env", "unknown"),
        "mcp_mounted": _mcp_mounted,
        "scheduler_enabled": bool(getattr(settings, "scheduler", None) and settings.scheduler.enabled),
        "scheduler_running": scheduler_running,
    }


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "notion-newsroom",
        "health": "/health",
        "mcp": "/mcp",
    }


