"""Tests for R4 — interrupt-all-on-shutdown in MessageOrchestrator.

Pre-fix, ``_active_requests`` lived only in memory. A SIGTERM to the
bot process ended the Python interpreter while the corresponding
Claude subprocesses continued running, and the "Stop" inline button
pointed at a dead process. The fix wires a graceful interrupt into
the shutdown path by setting every request's ``interrupt_event``.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from src.bot.orchestrator import ActiveRequest, MessageOrchestrator


@pytest.fixture
def orchestrator():
    # Settings / deps are not exercised by the interrupt helper, but
    # MessageOrchestrator requires them at construction.
    settings = MagicMock()
    settings.enable_project_threads = False
    return MessageOrchestrator(settings=settings, deps={})


class TestInterruptAllActiveRequests:
    def test_no_active_requests_returns_zero(self, orchestrator):
        assert orchestrator.interrupt_all_active_requests() == 0

    def test_sets_event_and_marks_interrupted(self, orchestrator):
        req = ActiveRequest(user_id=1)
        orchestrator._active_requests[1] = req

        assert req.interrupt_event.is_set() is False

        count = orchestrator.interrupt_all_active_requests()

        assert count == 1
        assert req.interrupt_event.is_set() is True
        assert req.interrupted is True

    def test_multiple_users_all_get_signalled(self, orchestrator):
        users = [1, 2, 3, 4, 5]
        requests = {u: ActiveRequest(user_id=u) for u in users}
        orchestrator._active_requests.update(requests)

        count = orchestrator.interrupt_all_active_requests()

        assert count == 5
        for req in requests.values():
            assert req.interrupt_event.is_set() is True
            assert req.interrupted is True

    def test_already_interrupted_request_is_skipped(self, orchestrator):
        """If a user has already pressed Stop the event is set — the
        shutdown helper must not double-count that request."""
        req = ActiveRequest(user_id=7)
        req.interrupt_event.set()
        req.interrupted = True
        orchestrator._active_requests[7] = req

        count = orchestrator.interrupt_all_active_requests()

        assert count == 0

    def test_mixed_already_interrupted_and_live(self, orchestrator):
        live = ActiveRequest(user_id=1)
        stopped = ActiveRequest(user_id=2)
        stopped.interrupt_event.set()
        stopped.interrupted = True

        orchestrator._active_requests[1] = live
        orchestrator._active_requests[2] = stopped

        count = orchestrator.interrupt_all_active_requests()

        # Only the live one is signalled — the Stop-button-pressed one
        # does not need re-interrupting.
        assert count == 1
        assert live.interrupt_event.is_set() is True


class TestIntegrationWithExecuteCommandWatcher:
    """Sanity check: the SDK watcher pattern (external event →
    subprocess interrupt) is the same one the Stop button uses, so
    we do not need to change any SDK code. This test documents that
    invariant — it asserts that an event set on the request is
    visible to any code observing the same event."""

    async def test_signalling_event_is_observable(self, orchestrator):
        req = ActiveRequest(user_id=42)
        orchestrator._active_requests[42] = req

        observed = asyncio.Event()

        async def watcher():
            await req.interrupt_event.wait()
            observed.set()

        task = asyncio.create_task(watcher())
        await asyncio.sleep(0)  # let watcher block on the event

        orchestrator.interrupt_all_active_requests()
        await asyncio.wait_for(observed.wait(), timeout=1.0)

        assert observed.is_set() is True
        task.cancel()
