"""Tests for ``src.utils.redaction`` (M5 from upgrade.md).

Before this module, the redaction helper lived inside the bot
orchestrator and was only applied to user-visible output. Every
``Claude CLI stderr`` log line and every error message that folded
stderr into ``ClaudeProcessError`` reached the structured log
pipeline raw — so a subprocess that printed ``KEY=<secret>`` would
leak that secret into journald / stdout.

These tests pin the redaction behaviour so future callers (and the
structlog processor) can rely on a stable contract.
"""

import pytest

from src.utils.redaction import redact_secrets, structlog_redaction_processor


class TestRedactCommonSecretShapes:
    @pytest.mark.parametrize(
        "payload,must_not_contain",
        [
            (
                "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
                "abcdefghijklmnopqrstuvwxyz0123456789",
            ),
            (
                "Authorization: Bearer abc123def456ghi789jkl012",
                "abc123def456ghi789jkl012",
            ),
            (
                "aws access key: AKIA0123ABCD4567WXYZ",
                "0123ABCD4567WXYZ",
            ),
            (
                "psql postgres://user:supersecretpw@db.internal:5432/app",
                "supersecretpw",
            ),
            (
                "--token mytoken-value-goes-here",
                "mytoken-value-goes-here",
            ),
            (
                "GITHUB_WEBHOOK_SECRET=abcdefghij123456",
                "abcdefghij123456",
            ),
            (
                "Basic dXNlcjpwYXNzd29yZA==",
                "dXNlcjpwYXNzd29yZA==",
            ),
        ],
    )
    def test_secret_is_removed(self, payload, must_not_contain):
        redacted = redact_secrets(payload)
        assert must_not_contain not in redacted
        assert "***" in redacted


class TestRedactPreservesStructure:
    def test_prefix_marker_is_kept(self):
        """Operators still need to see *that* a Bearer token was in the
        line, just not the value. The redaction preserves the marker."""
        out = redact_secrets("Authorization: Bearer veryl0ngs3cr3t0")
        assert "Bearer ***" in out
        assert "veryl0ngs3cr3t0" not in out

    def test_env_assignment_prefix_is_kept(self):
        out = redact_secrets("TOKEN=abcdefgh12345678")
        assert "TOKEN=***" in out
        assert "abcdefgh12345678" not in out

    def test_connection_string_redacts_password(self):
        """URL shape ``scheme://user:password@host`` — the password
        must not survive. The current regex also drops the ``://``
        and ``@`` delimiters along with the password (keeping the
        ``user:`` prefix as the marker); that's acceptable as long
        as the secret itself is gone."""
        out = redact_secrets("postgres://alice:veryl0ngs3cret@db.prod:5432/app")
        assert "veryl0ngs3cret" not in out
        assert "alice:" in out  # user name kept as marker
        assert "***" in out


class TestBenignInputsUntouched:
    @pytest.mark.parametrize(
        "benign",
        [
            "",
            "Plain log line with no secrets",
            "Filed a PR at github.com/org/repo/pull/42",
            "Stack trace line: File 'foo.py', line 17",
        ],
    )
    def test_no_marker_inserted_when_nothing_to_redact(self, benign):
        assert redact_secrets(benign) == benign


class TestNonStringInputs:
    def test_none_is_empty_string(self):
        assert redact_secrets(None) == ""  # type: ignore[arg-type]

    def test_int_is_stringified(self):
        assert redact_secrets(42) == "42"  # type: ignore[arg-type]


class TestStructlogProcessor:
    def test_processor_scrubs_top_level_string_values(self):
        event_dict = {
            "event": "outgoing request",
            "auth_header": "Authorization: Bearer my-secret-token-value-here",
            "user_id": 99,
        }

        out = structlog_redaction_processor(None, "info", dict(event_dict))
        assert "my-secret-token-value-here" not in out["auth_header"]
        assert "Bearer ***" in out["auth_header"]
        # Non-string values pass through untouched.
        assert out["user_id"] == 99
        assert out["event"] == "outgoing request"

    def test_processor_scrubs_list_of_strings(self):
        event_dict = {
            "event": "stderr dump",
            "lines": [
                "startup ok",
                "ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            ],
        }

        out = structlog_redaction_processor(None, "warning", dict(event_dict))
        assert any("***" in line for line in out["lines"])
        for line in out["lines"]:
            assert "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in line

    def test_processor_walks_into_nested_dicts_one_level(self):
        """The processor scrubs strings that live one container-level
        below the top event_dict — i.e. a dict-as-value gets its
        string entries redacted. Deeper nesting is returned as-is to
        avoid runaway recursion on cyclic structures."""
        event_dict = {"meta": {"nested_secret": "TOKEN=abcdefg12345"}}
        out = structlog_redaction_processor(None, "info", dict(event_dict))
        assert "abcdefg12345" not in out["meta"]["nested_secret"]
        assert "TOKEN=***" in out["meta"]["nested_secret"]

        # Two levels down does NOT get scrubbed — by design.
        deep = {"outer": {"mid": {"leaf": "TOKEN=abcdefg12345"}}}
        out2 = structlog_redaction_processor(None, "info", dict(deep))
        assert out2["outer"]["mid"]["leaf"] == "TOKEN=abcdefg12345"
