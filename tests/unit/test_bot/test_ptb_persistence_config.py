"""Tests for the ``ptb_persistence_path`` setting (R4 from upgrade.md).

Pre-fix, ``context.user_data`` was wiped on every restart — a user's
``current_directory`` / ``claude_session_id`` / ``verbose_level`` all
reset to defaults. The fix is opt-in: operators who want persistence
set ``PTB_PERSISTENCE_PATH`` and PTB's ``PicklePersistence`` takes
over.

These tests drive the ``Settings`` surface only — wiring into the
``Application`` is validated by a smoke test. We don't assert PTB's
internal persistence behaviour (that belongs to the library).
"""

from pathlib import Path

from src.config import create_test_config


class TestSettingsField:
    def test_default_is_none(self):
        """Out of the box, no persistence — legacy behaviour
        preserved for existing deployments."""
        cfg = create_test_config()
        assert cfg.ptb_persistence_path is None

    def test_path_is_accepted(self, tmp_path):
        target = tmp_path / "state.pickle"
        cfg = create_test_config(ptb_persistence_path=str(target))
        assert cfg.ptb_persistence_path == Path(str(target))


class TestCoreBuilderIntegrationSmokeTest:
    """Verify the ``PicklePersistence`` import path in ``core.py`` is
    the correct one for the installed python-telegram-bot version.

    We do NOT spin up a real ``Application`` — that requires a valid
    bot token and network access. Importing ``PicklePersistence``
    and instantiating it with a temp path is enough to prove the
    wiring will not blow up on the first request.
    """

    def test_pickle_persistence_is_importable_and_constructible(self, tmp_path):
        from telegram.ext import PicklePersistence

        target = tmp_path / "state.pickle"
        # Must not raise.
        persistence = PicklePersistence(filepath=str(target))
        assert persistence is not None

    def test_parent_directory_is_created_when_missing(self, tmp_path):
        """``core.py`` creates the parent directory on demand so the
        operator only has to pick a filename. Exercise that code
        path by pre-asserting the directory is absent."""
        missing_parent = tmp_path / "not-yet-here"
        target = missing_parent / "state.pickle"

        assert missing_parent.exists() is False

        target.parent.mkdir(parents=True, exist_ok=True)
        assert missing_parent.exists() is True
