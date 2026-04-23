"""Tests for the webhook API server bind host (M4 from upgrade.md).

Pre-fix, ``run_api_server`` hardcoded ``host="0.0.0.0"``. Combined
with the pre-C2 default of ``DEVELOPMENT_MODE=true`` (which opens up
``/docs``), this meant any host without a firewall exposed internal
docs + the webhook endpoints to the public internet on port 8080.

The fix: drive the bind from ``settings.api_server_host``, defaulting
to loopback (``127.0.0.1``). Operators who want it public set the
env var and put a reverse proxy in front.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api import server as server_module
from src.config import create_test_config
from src.events.bus import EventBus


class TestApiServerHostDefault:
    def test_settings_default_is_loopback(self):
        """The Pydantic default must be loopback — the whole point of
        the fix."""
        cfg = create_test_config()
        assert cfg.api_server_host == "127.0.0.1"

    def test_settings_can_be_overridden(self):
        """Operators running behind a reverse proxy explicitly opt in
        to a public bind."""
        cfg = create_test_config(api_server_host="0.0.0.0")
        assert cfg.api_server_host == "0.0.0.0"


class TestRunApiServerUsesConfiguredHost:
    """``uvicorn.Config`` must receive the configured host verbatim —
    a regression to the old hardcoded ``0.0.0.0`` would silently
    reintroduce the exposure."""

    @pytest.mark.parametrize(
        "configured_host",
        ["127.0.0.1", "0.0.0.0", "10.0.1.5"],
    )
    async def test_uvicorn_config_receives_host(self, configured_host):
        settings = create_test_config(
            api_server_host=configured_host,
            api_server_port=9999,
        )

        captured: dict = {}

        class _FakeServer:
            def __init__(self, config):
                captured["host"] = config.host
                captured["port"] = config.port

            async def serve(self):
                return None

        # Patch the uvicorn symbols inside the ``server`` module's
        # namespace. ``run_api_server`` imports uvicorn locally so we
        # patch on the module path it uses.
        fake_uvicorn = MagicMock()
        fake_uvicorn.Config = MagicMock(
            side_effect=lambda **kwargs: MagicMock(
                host=kwargs["host"], port=kwargs["port"]
            )
        )

        def fake_config(**kwargs):
            captured["host"] = kwargs["host"]
            captured["port"] = kwargs["port"]
            cfg_obj = MagicMock()
            cfg_obj.host = kwargs["host"]
            cfg_obj.port = kwargs["port"]
            return cfg_obj

        fake_uvicorn.Config = fake_config
        fake_uvicorn.Server = lambda cfg: _FakeServer(cfg)

        with patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
            with patch.object(
                server_module, "create_api_app", return_value=MagicMock()
            ):
                await server_module.run_api_server(
                    event_bus=EventBus(),
                    settings=settings,
                    db_manager=None,
                )

        assert captured["host"] == configured_host
        assert captured["port"] == 9999


class TestBindIsLogged:
    """An explicit startup log line makes an accidental 0.0.0.0
    bind obvious at boot rather than a silent exposure."""

    async def test_startup_log_includes_host_and_port(self, caplog):
        import logging

        settings = create_test_config(
            api_server_host="127.0.0.1",
            api_server_port=8888,
        )

        fake_uvicorn = MagicMock()
        fake_uvicorn.Config = lambda **kwargs: MagicMock(**kwargs)

        class _NoopServer:
            def __init__(self, cfg):
                pass

            async def serve(self):
                return None

        fake_uvicorn.Server = _NoopServer

        caplog.set_level(logging.INFO)
        with patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
            with patch.object(
                server_module, "create_api_app", return_value=MagicMock()
            ):
                with patch.object(
                    server_module.logger, "info", wraps=server_module.logger.info
                ) as log_info:
                    await server_module.run_api_server(
                        event_bus=EventBus(),
                        settings=settings,
                        db_manager=AsyncMock(),
                    )

        # At least one info call mentions our host + port
        calls = [c.kwargs for c in log_info.call_args_list if c.kwargs]
        matching = [
            c for c in calls if c.get("host") == "127.0.0.1" and c.get("port") == 8888
        ]
        assert matching, f"expected startup log with host/port; saw {calls}"
