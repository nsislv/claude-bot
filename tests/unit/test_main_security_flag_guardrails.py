"""Tests for ``enforce_security_flag_guardrails`` (H1 from upgrade.md).

``DISABLE_SECURITY_PATTERNS`` turns off path-traversal / shell-pattern
validation; ``DISABLE_TOOL_VALIDATION`` turns off the Claude tool
allowlist. A single typo in a deployment config used to be enough to
silently neuter the security model. Startup now refuses to proceed
without an explicit ``I_UNDERSTAND_SECURITY_IS_DISABLED=true`` opt-in
and logs a ``critical``-level warning when the bot boots with either
flag set.
"""

from unittest.mock import MagicMock

import pytest

from src.exceptions import ConfigurationError
from src.main import enforce_security_flag_guardrails


def _config(**overrides):
    """Build a minimal Settings-shaped mock for the guardrail rules."""
    cfg = MagicMock()
    cfg.disable_security_patterns = overrides.pop("disable_security_patterns", False)
    cfg.disable_tool_validation = overrides.pop("disable_tool_validation", False)
    cfg.i_understand_security_is_disabled = overrides.pop(
        "i_understand_security_is_disabled", False
    )
    assert not overrides, f"unexpected overrides: {overrides}"
    return cfg


class TestDefaultsPassThrough:
    def test_all_flags_default_false_is_noop(self):
        """The normal prod config must not raise or log critical."""
        logger = MagicMock()
        enforce_security_flag_guardrails(_config(), logger)
        assert not logger.critical.called


class TestRefuseWithoutExplicitOptIn:
    def test_disable_security_patterns_without_opt_in_raises(self):
        with pytest.raises(ConfigurationError) as exc:
            enforce_security_flag_guardrails(
                _config(disable_security_patterns=True),
                MagicMock(),
            )
        message = str(exc.value)
        assert "DISABLE_SECURITY_PATTERNS" in message
        assert "I_UNDERSTAND_SECURITY_IS_DISABLED" in message

    def test_disable_tool_validation_without_opt_in_raises(self):
        with pytest.raises(ConfigurationError) as exc:
            enforce_security_flag_guardrails(
                _config(disable_tool_validation=True),
                MagicMock(),
            )
        message = str(exc.value)
        assert "DISABLE_TOOL_VALIDATION" in message
        assert "I_UNDERSTAND_SECURITY_IS_DISABLED" in message

    def test_both_flags_without_opt_in_raises_and_names_both(self):
        with pytest.raises(ConfigurationError) as exc:
            enforce_security_flag_guardrails(
                _config(
                    disable_security_patterns=True,
                    disable_tool_validation=True,
                ),
                MagicMock(),
            )
        message = str(exc.value)
        assert "DISABLE_SECURITY_PATTERNS" in message
        assert "DISABLE_TOOL_VALIDATION" in message


class TestExplicitOptInStartsWithCriticalLog:
    def test_opt_in_with_security_patterns_starts_and_logs_critical(self):
        logger = MagicMock()
        enforce_security_flag_guardrails(
            _config(
                disable_security_patterns=True,
                i_understand_security_is_disabled=True,
            ),
            logger,
        )

        assert (
            logger.critical.called
        ), "expected a critical-level log when a disable flag is honoured"
        message = logger.critical.call_args.args[0]
        assert "DISABLE_SECURITY_PATTERNS" in message
        assert "Do NOT run in production" in message

    def test_opt_in_with_tool_validation_starts_and_logs_critical(self):
        logger = MagicMock()
        enforce_security_flag_guardrails(
            _config(
                disable_tool_validation=True,
                i_understand_security_is_disabled=True,
            ),
            logger,
        )

        assert logger.critical.called
        message = logger.critical.call_args.args[0]
        assert "DISABLE_TOOL_VALIDATION" in message

    def test_opt_in_without_any_disable_flag_does_not_log(self):
        """Setting the opt-in without any disable flag is not a warning
        — it is an inert config (nothing is disabled)."""
        logger = MagicMock()
        enforce_security_flag_guardrails(
            _config(i_understand_security_is_disabled=True),
            logger,
        )
        assert not logger.critical.called
