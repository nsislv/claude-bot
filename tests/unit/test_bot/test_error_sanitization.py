"""M7 from upgrade.md — user-facing error messages must not leak
internals.

Pre-fix, ``_format_process_error`` and the generic ``ClaudeError``
fallback both interpolated the raw exception string directly into
the message shown to the user. Because ``ClaudeProcessError.args``
can carry subprocess stderr (file paths, argv slices, CLI output),
that text reached the end user untouched.

These tests pin the new generic-message contract for both paths.
"""

from src.bot.handlers.message import _format_error_message, _format_process_error
from src.claude.exceptions import ClaudeError, ClaudeProcessError


class TestFormatProcessErrorIsGeneric:
    def test_subprocess_stderr_is_not_leaked(self):
        """A ``ClaudeProcessError`` carrying a real stderr dump must
        not reproduce any of the file paths / commands / credentials
        in the user-facing output."""
        stderr_dump = (
            "Traceback: File '/home/user/.ssh/id_rsa', line 42, in <module>\n"
            "Stderr: ANTHROPIC_API_KEY=sk-ant-api03-0123456789abcdef\n"
            "exit code 127: /usr/local/bin/some-internal-binary not found"
        )

        msg = _format_process_error(stderr_dump)

        assert "id_rsa" not in msg
        assert "ANTHROPIC_API_KEY" not in msg
        assert "sk-ant-api03-0123456789abcdef" not in msg
        assert "/usr/local/bin/some-internal-binary" not in msg

    def test_generic_actionable_guidance_present(self):
        """The user still needs to know WHAT to do — the message
        keeps the "try again / /new / /status" guidance."""
        msg = _format_process_error("anything")
        assert "Try your request again" in msg
        assert "/new" in msg
        assert "/status" in msg
        assert "administrator" in msg.lower()

    def test_shape_remains_html(self):
        """Downstream senders use parse_mode=HTML — the <b> tag must
        survive the scrubbing."""
        msg = _format_process_error("x")
        assert "<b>" in msg
        assert "</b>" in msg


class TestFormatErrorMessageForClaudeProcessError:
    def test_dispatch_routes_to_generic_output(self):
        """``_format_error_message`` with a ``ClaudeProcessError``
        must route through ``_format_process_error`` — the raw
        exception string from the dispatch path must also not
        leak."""
        err = ClaudeProcessError(
            "Claude process error: "
            "cat /etc/shadow: Permission denied\n"
            "Stderr: AKIA0123ABCD4567WXYZ found in env"
        )

        msg = _format_error_message(err)

        assert "/etc/shadow" not in msg
        assert "AKIA" not in msg
        # And we still show a Claude-Process-Error heading so the user
        # knows the category of failure.
        assert "Claude Process Error" in msg


class TestFormatErrorMessageForGenericClaudeError:
    def test_unknown_claude_error_subtype_produces_generic_message(self):
        """A subclass of ``ClaudeError`` we don't explicitly handle
        must ALSO not leak its str representation."""

        class MysteryClaudeError(ClaudeError):
            pass

        err = MysteryClaudeError(
            "Internal diagnostic: /opt/secret-binary crashed with "
            "TOKEN=supersecretval9999"
        )

        msg = _format_error_message(err)

        assert "/opt/secret-binary" not in msg
        assert "supersecretval9999" not in msg
        assert "TOKEN=" not in msg
        assert "Claude Error" in msg


class TestSpecificUserFriendlyErrorsKeepGuidance:
    """Regression guard — the explicit timeout / MCP / parsing /
    session branches shouldn't accidentally be swallowed by the
    generic-scrub change (they remain informative).
    """

    def test_rate_limit_hint_still_reaches_user(self):
        msg = _format_error_message("rate limit reached, please retry")
        assert "Rate Limit" in msg or "rate limit" in msg.lower()

    def test_timeout_string_still_maps_to_timeout_card(self):
        msg = _format_error_message("Claude SDK timed out after 300s")
        assert "Timeout" in msg
