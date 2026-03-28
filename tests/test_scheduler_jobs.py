from __future__ import annotations

import asyncio
from typing import Any

import pytest

from newsroom.scheduler import jobs


def _reset_scheduler_state() -> None:
    jobs._state.initialized = False
    jobs._state.page_statuses.clear()
    jobs._queued_work_items.clear()
    jobs._workflow_queue = None


def test_enqueue_workflow_dedupes_items() -> None:
    _reset_scheduler_state()
    jobs._ensure_workflow_queue(queue_size=10)

    assert jobs._enqueue_workflow("page-1", "Researching") is True
    assert jobs._enqueue_workflow("page-1", "Researching") is False
    assert jobs._workflow_queue is not None
    assert jobs._workflow_queue.qsize() == 1


@pytest.mark.asyncio
async def test_poll_and_dispatch_enqueues_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_scheduler_state()
    jobs._ensure_workflow_queue(queue_size=10)

    async def fake_fetch_story_pages(notion: Any, settings: Any) -> list[dict[str, Any]]:
        del notion, settings
        return [
            {
                "id": "page-1",
                "properties": {
                    "Status": {
                        "type": "status",
                        "status": {"name": "Researching"},
                    }
                },
            }
        ]

    async def fake_traffic_cycle() -> dict[str, Any]:
        return {"ok": True, "trending_count": 0, "trending": []}

    # First cycle initializes snapshot.
    monkeypatch.setattr(jobs, "_fetch_story_pages", fake_fetch_story_pages)
    monkeypatch.setattr(jobs, "_run_traffic_cycle", fake_traffic_cycle)
    first = await jobs.poll_and_dispatch()
    assert first["status_changes"] == 0

    # Second cycle sees no status change and should not enqueue.
    second = await jobs.poll_and_dispatch()
    assert second["status_changes"] == 0
    assert second["enqueued"] == 0

    # Third cycle simulates a new status transition and enqueues.
    async def fake_fetch_story_pages_changed(notion: Any, settings: Any) -> list[dict[str, Any]]:
        del notion, settings
        return [
            {
                "id": "page-1",
                "properties": {
                    "Status": {
                        "type": "status",
                        "status": {"name": "Needs Audit"},
                    }
                },
            }
        ]

    monkeypatch.setattr(jobs, "_fetch_story_pages", fake_fetch_story_pages_changed)
    third = await jobs.poll_and_dispatch()
    assert third["status_changes"] == 1
    assert third["enqueued"] == 1
    assert third["queue_size"] == 1


@pytest.mark.asyncio
async def test_enqueue_skips_when_queue_full() -> None:
    _reset_scheduler_state()
    queue = jobs._ensure_workflow_queue(queue_size=1)
    queue.put_nowait(("existing", "Researching"))

    assert jobs._enqueue_workflow("page-2", "Researching") is False
