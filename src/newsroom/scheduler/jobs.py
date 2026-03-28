"""Background polling jobs for Newsroom OS workflows."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from newsroom.config import Settings, get_settings
from newsroom.constants import (
    STATUS_APPROVED_FOR_PUBLICATION,
    STATUS_NEEDS_AUDIT,
    STATUS_RESEARCHING,
    get_logger,
)
from newsroom.notion.client import NotionClient
from newsroom.types import SchedulerError
from newsroom.workflows.agency_bridge import prepare_for_publication
from newsroom.workflows.context_hunter import run_context_hunter
from newsroom.workflows.narrative_auditor import run_narrative_audit
from newsroom.workflows.traffic_strategist import detect_trending_stories

logger = get_logger(__name__, component="scheduler")

TARGET_STATUSES = {
    STATUS_RESEARCHING: run_context_hunter,
    STATUS_NEEDS_AUDIT: run_narrative_audit,
    STATUS_APPROVED_FOR_PUBLICATION: prepare_for_publication,
}

WorkflowFn = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class PollState:
    """In-memory polling state for change detection and de-duplication."""

    initialized: bool = False
    page_statuses: dict[str, str] = field(default_factory=dict)


_state = PollState()
_scheduler: AsyncIOScheduler | None = None
_poll_lock = asyncio.Lock()
_page_locks: dict[str, asyncio.Lock] = {}
_workflow_queue: asyncio.Queue[tuple[str, str]] | None = None
_worker_tasks: list[asyncio.Task[Any]] = []
_queued_work_items: set[tuple[str, str]] = set()


def _get_page_lock(page_id: str) -> asyncio.Lock:
    lock = _page_locks.get(page_id)
    if lock is None:
        lock = asyncio.Lock()
        _page_locks[page_id] = lock
    return lock


def _extract_status_name(page: dict[str, Any]) -> str | None:
    properties = page.get("properties", {}) if isinstance(page, dict) else {}
    if not isinstance(properties, dict):
        return None

    status_prop = properties.get("Status")
    if isinstance(status_prop, dict) and status_prop.get("type") == "status":
        status_data = status_prop.get("status", {})
        if isinstance(status_data, dict):
            name = status_data.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()

    for prop in properties.values():
        if not isinstance(prop, dict) or prop.get("type") != "status":
            continue
        status_data = prop.get("status", {})
        if isinstance(status_data, dict):
            name = status_data.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


async def _fetch_story_pages(notion: NotionClient, settings: Settings) -> list[dict[str, Any]]:
    pages = await notion.query_database(database_id=settings.notion.database_id, page_size=100)
    logger.info("scheduler_polled_pages database_id=%s count=%s", settings.notion.database_id, len(pages))
    return pages


def _compute_changed_candidates(pages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    changed: list[tuple[str, str]] = []

    for page in pages:
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id.strip():
            continue

        status = _extract_status_name(page)
        if not status:
            continue

        previous = _state.page_statuses.get(page_id)
        _state.page_statuses[page_id] = status

        if not _state.initialized:
            continue

        if status in TARGET_STATUSES and previous != status:
            changed.append((page_id, status))

    if not _state.initialized:
        _state.initialized = True
        logger.info("scheduler_initial_snapshot pages=%s", len(_state.page_statuses))

    return changed


async def _run_workflow_for_page(page_id: str, status: str) -> dict[str, Any]:
    workflow = TARGET_STATUSES.get(status)
    if workflow is None:
        return {"ok": False, "page_id": page_id, "status": status, "error": "Unknown status mapping"}

    page_lock = _get_page_lock(page_id)
    if page_lock.locked():
        logger.warning("scheduler_skip_locked_page page_id=%s status=%s", page_id, status)
        return {"ok": False, "page_id": page_id, "status": status, "error": "Page lock busy"}

    async with page_lock:
        try:
            logger.info("scheduler_workflow_start page_id=%s status=%s workflow=%s", page_id, status, workflow.__name__)
            result = await asyncio.wait_for(workflow(page_id), timeout=300)
            logger.info("scheduler_workflow_success page_id=%s status=%s", page_id, status)
            return {"ok": True, "page_id": page_id, "status": status, "result": result}
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduler_workflow_failed page_id=%s status=%s error=%s", page_id, status, exc)
            wrapped = SchedulerError(f"Workflow dispatch failed for page {page_id} in status {status}: {exc}")
            return {"ok": False, "page_id": page_id, "status": status, "error": str(wrapped)}


def _ensure_workflow_queue(queue_size: int) -> asyncio.Queue[tuple[str, str]]:
    global _workflow_queue
    if _workflow_queue is None:
        _workflow_queue = asyncio.Queue(maxsize=queue_size)
    return _workflow_queue


def _enqueue_workflow(page_id: str, status: str) -> bool:
    if not page_id.strip() or not status.strip():
        return False

    queue = _workflow_queue
    if queue is None:
        logger.warning("scheduler_enqueue_skipped reason=queue_not_initialized page_id=%s status=%s", page_id, status)
        return False

    item = (page_id, status)
    if item in _queued_work_items:
        logger.info("scheduler_enqueue_deduped page_id=%s status=%s", page_id, status)
        return False

    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        logger.warning("scheduler_enqueue_skipped reason=queue_full page_id=%s status=%s", page_id, status)
        return False

    _queued_work_items.add(item)
    logger.info("scheduler_enqueued_workflow page_id=%s status=%s queue_size=%s", page_id, status, queue.qsize())
    return True


async def _workflow_worker(worker_id: int) -> None:
    queue = _workflow_queue
    if queue is None:
        logger.warning("scheduler_worker_exit reason=queue_not_initialized worker_id=%s", worker_id)
        return

    logger.info("scheduler_worker_started worker_id=%s", worker_id)
    try:
        while True:
            page_id, status = await queue.get()
            item = (page_id, status)
            _queued_work_items.discard(item)
            try:
                await _run_workflow_for_page(page_id=page_id, status=status)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "scheduler_worker_unexpected_error worker_id=%s page_id=%s status=%s error=%s",
                    worker_id,
                    page_id,
                    status,
                    exc,
                )
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        logger.info("scheduler_worker_stopped worker_id=%s", worker_id)
        raise


async def _run_traffic_cycle() -> dict[str, Any]:
    try:
        trending = await asyncio.wait_for(detect_trending_stories(), timeout=180)
        logger.info("scheduler_traffic_cycle trending_count=%s", len(trending))
        return {"ok": True, "trending_count": len(trending), "trending": trending}
    except Exception as exc:  # noqa: BLE001
        logger.exception("scheduler_traffic_cycle_failed error=%s", exc)
        return {"ok": False, "error": str(exc), "trending_count": 0, "trending": []}


async def poll_and_dispatch() -> dict[str, Any]:
    """Poll story database and dispatch status-based newsroom workflows."""

    if _poll_lock.locked():
        logger.warning("scheduler_poll_skipped reason=lock_busy")
        return {"ok": False, "skipped": True, "reason": "poll_lock_busy"}

    async with _poll_lock:
        settings = get_settings()
        notion = NotionClient(settings=settings)
        summary: dict[str, Any] = {
            "ok": True,
            "polled_pages": 0,
            "status_changes": 0,
            "workflow_runs": [],
            "enqueued": 0,
            "skipped_duplicates": 0,
            "queue_size": 0,
            "traffic": {},
        }

        try:
            pages = await _fetch_story_pages(notion=notion, settings=settings)
            summary["polled_pages"] = len(pages)

            changed = _compute_changed_candidates(pages)
            summary["status_changes"] = len(changed)

            traffic_task = asyncio.create_task(_run_traffic_cycle())

            if changed:
                enqueued = 0
                skipped_duplicates = 0
                for page_id, status in changed:
                    if _enqueue_workflow(page_id=page_id, status=status):
                        enqueued += 1
                    else:
                        skipped_duplicates += 1
                summary["enqueued"] = enqueued
                summary["skipped_duplicates"] = skipped_duplicates
            else:
                logger.info("scheduler_no_status_changes")

            if _workflow_queue is not None:
                summary["queue_size"] = _workflow_queue.qsize()

            summary["traffic"] = await traffic_task
            logger.info(
                "scheduler_cycle_completed polled=%s changes=%s enqueued=%s queue_size=%s",
                summary["polled_pages"],
                summary["status_changes"],
                summary["enqueued"],
                summary["queue_size"],
            )
            return summary
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduler_cycle_failed error=%s", exc)
            summary["ok"] = False
            wrapped = SchedulerError(f"Scheduler polling cycle failed: {exc}")
            summary["error"] = str(wrapped)
            return summary


def get_scheduler() -> AsyncIOScheduler:
    """Return singleton AsyncIOScheduler instance."""

    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def start_scheduler(interval_seconds: int = 120) -> AsyncIOScheduler:
    """Start background scheduler with polling and workflow dispatch every interval."""

    scheduler = get_scheduler()
    if scheduler.running:
        return scheduler

    scheduler.add_job(
        poll_and_dispatch,
        trigger="interval",
        seconds=max(30, interval_seconds),
        id="newsroom-polling",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    settings = get_settings()
    queue = _ensure_workflow_queue(settings.scheduler.workflow_queue_size)

    scheduler.start()
    if not _worker_tasks:
        for worker_id in range(1, settings.scheduler.workflow_worker_count + 1):
            _worker_tasks.append(asyncio.create_task(_workflow_worker(worker_id=worker_id)))

    logger.info("scheduler_started interval_seconds=%s", max(30, interval_seconds))
    logger.info(
        "scheduler_workers_started workers=%s queue_size_limit=%s",
        settings.scheduler.workflow_worker_count,
        queue.maxsize,
    )
    return scheduler


def shutdown_scheduler(wait: bool = False) -> None:
    """Shutdown scheduler safely."""

    global _scheduler
    global _workflow_queue

    if _worker_tasks:
        if wait and _workflow_queue is not None:
            logger.info("scheduler_queue_shutdown queue_size=%s", _workflow_queue.qsize())
        for task in _worker_tasks:
            task.cancel()
        for task in _worker_tasks:
            with suppress(Exception):
                task.result()
        _worker_tasks.clear()
        logger.info("scheduler_workers_shutdown")

    _queued_work_items.clear()
    _workflow_queue = None

    if _scheduler is None:
        return
    if _scheduler.running:
        _scheduler.shutdown(wait=wait)
        logger.info("scheduler_shutdown wait=%s", wait)
    _scheduler = None
