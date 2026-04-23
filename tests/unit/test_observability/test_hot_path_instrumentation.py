"""End-to-end tests proving the R5 metrics are wired to their
production hot paths.

Follow-up to ``feat/r5-prometheus-metrics`` where the metrics were
registered but the DB-query / active-session instrumentation was
flagged as a follow-up. This file is the regression guard against
that follow-up silently coming un-wired in the future.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.observability.metrics import BotMetrics, MetricsRegistry
from src.storage.database import DatabaseManager


@pytest.fixture
def fresh_metrics(monkeypatch):
    """Swap the module-level singleton for a clean registry per test
    so observations from other tests don't bleed into assertions."""
    from src.observability import metrics as metrics_module

    fresh = BotMetrics(MetricsRegistry())
    monkeypatch.setattr(metrics_module, "bot_metrics", fresh)
    # Also update any already-imported references.
    monkeypatch.setattr("src.observability.bot_metrics", fresh)
    return fresh


class TestDbQueryLatencyInstrumented:
    async def test_get_connection_observes_latency(self, fresh_metrics):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "hot-path.db"
            manager = DatabaseManager(f"sqlite:///{db_path}")
            await manager.initialize()

            try:
                async with manager.get_connection() as conn:
                    await conn.execute("SELECT 1")
            finally:
                await manager.close()

        body = await fresh_metrics.registry.render()

        # At least one observation landed on the histogram. The
        # migration runner + health check path exercise the
        # contextmanager multiple times at init, so count is >= 1.
        assert "bot_db_query_latency_seconds_count" in body
        # A fully-zero count line would be ``_count 0``, which we
        # want to fail because it'd mean the instrumentation is
        # dead.
        assert "bot_db_query_latency_seconds_count 0" not in body
        assert "bot_db_query_latency_seconds_sum" in body


class TestActiveSessionsGauge:
    """SessionManager.publish keeps the gauge in sync with the
    in-memory ``active_sessions`` dict."""

    async def test_gauge_tracks_addition_and_removal(self, fresh_metrics):
        from src.claude.session import SessionManager

        # Minimal config / storage doubles — the gauge only cares
        # about dict-size, not real session state.
        config = MagicMock()
        storage = MagicMock()
        storage.save_session = MagicMock()
        storage.delete_session = MagicMock()

        async def _noop(*args, **kwargs):  # noqa: ARG001
            return None

        storage.save_session = _noop  # type: ignore[assignment]
        storage.delete_session = _noop  # type: ignore[assignment]

        manager = SessionManager(config, storage)

        # Directly mutate the dict + call the publisher (the real
        # code does both in the same statement — this mirrors that).
        manager.active_sessions["a"] = MagicMock(session_id="a")
        await manager._publish_active_sessions_metric()

        body_after_add = await fresh_metrics.registry.render()
        assert "bot_active_sessions 1.0" in body_after_add

        # Add another
        manager.active_sessions["b"] = MagicMock(session_id="b")
        await manager._publish_active_sessions_metric()

        body_after_two = await fresh_metrics.registry.render()
        assert "bot_active_sessions 2.0" in body_after_two

        # Remove one via remove_session (the production mutation
        # path).
        await manager.remove_session("a")

        body_after_remove = await fresh_metrics.registry.render()
        assert "bot_active_sessions 1.0" in body_after_remove

    async def test_publisher_failure_does_not_break_session_ops(
        self, fresh_metrics, monkeypatch
    ):
        """If the gauge explodes for any reason, session management
        must still work. This is the non-negotiable invariant."""
        from src.claude.session import SessionManager

        config = MagicMock()
        storage = MagicMock()

        async def _noop(*args, **kwargs):  # noqa: ARG001
            return None

        storage.save_session = _noop  # type: ignore[assignment]
        storage.delete_session = _noop  # type: ignore[assignment]

        manager = SessionManager(config, storage)
        manager.active_sessions["a"] = MagicMock(session_id="a")

        # Break the gauge.
        def _boom(*args, **kwargs):  # noqa: ARG001
            raise RuntimeError("gauge is broken")

        monkeypatch.setattr(fresh_metrics.active_sessions, "set", _boom)

        # Must not raise.
        await manager._publish_active_sessions_metric()


class TestDbMetricsFailureIsolation:
    async def test_broken_metric_does_not_break_connection_return(
        self, fresh_metrics, monkeypatch
    ):
        """Pool must always get the connection back, even if the
        histogram observation raises."""

        def _boom(*args, **kwargs):  # noqa: ARG001
            raise RuntimeError("histogram is broken")

        monkeypatch.setattr(fresh_metrics.db_query_latency_seconds, "observe", _boom)

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "isolation.db"
            manager = DatabaseManager(f"sqlite:///{db_path}")
            await manager.initialize()

            try:
                # Before the fix, a broken histogram call inside
                # the contextmanager's finally would leak the
                # connection. We exercise the path twice to prove
                # the pool is not drained.
                for _ in range(2):
                    async with manager.get_connection() as conn:
                        await conn.execute("SELECT 1")

                # Health check uses the same pool; if the pool
                # leaked we'd see it here.
                assert await manager.health_check() is True
            finally:
                await manager.close()
