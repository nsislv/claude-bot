"""Test custom exceptions."""

import pytest

from src.exceptions import (
    AuthenticationError,
    ClaudeCodeTelegramError,
    ConfigurationError,
    SecurityError,
)


def test_base_exception():
    """Test base exception."""
    with pytest.raises(ClaudeCodeTelegramError):
        raise ClaudeCodeTelegramError("Test error")


@pytest.mark.parametrize(
    "raised, caught_as",
    [
        (ConfigurationError("Config error"), ClaudeCodeTelegramError),
        (ConfigurationError("Config error"), ConfigurationError),
    ],
)
def test_configuration_error(raised, caught_as):
    """Test configuration error inheritance."""
    with pytest.raises(caught_as):
        raise raised


@pytest.mark.parametrize(
    "raised, caught_as",
    [
        (SecurityError("Security error"), ClaudeCodeTelegramError),
        (AuthenticationError("Auth error"), SecurityError),
    ],
)
def test_security_error(raised, caught_as):
    """Test security error inheritance."""
    with pytest.raises(caught_as):
        raise raised
