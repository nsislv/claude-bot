"""Best-effort redaction of credentials in free-form text.

M5 from ``upgrade.md``. Pattern-based redaction was previously only
applied to user-visible bot output in ``src/bot/orchestrator.py``.
The same patterns need to run over:

- every ``Claude CLI stderr`` log line the SDK integration emits,
- every ``captured_stderr`` slice folded into a
  ``ClaudeProcessError`` message, and
- any future log sink that might receive untrusted text.

This module holds the patterns + the ``redact_secrets`` helper in a
single place so every caller agrees on the ruleset. Regex-based
detection is strictly best-effort — it catches the common shapes
(``sk-ant-…``, ``ghp_…``, ``Bearer …``, ``KEY=value`` assignments,
connection strings) but will miss one-off custom formats. Structural
fixes (env-var scoping, credential vault) are the real answer;
redaction is the belt-and-braces layer for logs that reach operators.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List

# Patterns that look like secrets / credentials in free-form strings.
#
# Each pattern captures a deterministic *prefix* in a group so
# ``_redact_secrets`` can preserve the marker (e.g. ``Bearer ***`` or
# ``TOKEN=***``) while destroying the value. Keeping the prefix makes
# the redaction trail obvious in logs without resurrecting the secret.
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_...,
    # github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials in ``text`` with ``<prefix>***``.

    Non-string inputs are stringified first (important for structlog
    kwargs that can contain ints / paths / exceptions).
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


def structlog_redaction_processor(
    _logger: Any, _method_name: str, event_dict: dict
) -> dict:
    """structlog processor that runs ``redact_secrets`` over every value.

    Slots into the processor chain so even callers who forget to
    redact an ad-hoc stderr dump get it filtered before the record
    reaches stdout / syslog / journald.

    Applies to string values only; containers are walked one level
    deep so ``logger.warning("x", details=stderr_lines)`` still scrubs
    each line. We deliberately do NOT recurse indefinitely — a
    runaway recursion on cyclic structures is a worse failure than a
    missed redaction in nested data.
    """
    for key, value in list(event_dict.items()):
        event_dict[key] = _redact_value(value)
    return event_dict


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, (list, tuple)):
        redacted = [_redact_one_level(item) for item in value]
        return type(value)(redacted)
    if isinstance(value, dict):
        return {k: _redact_one_level(v) for k, v in value.items()}
    return value


def _redact_one_level(value: Any) -> Any:
    # Single-level walk: strings get scrubbed, everything else
    # returned as-is so we never recurse into a cycle.
    if isinstance(value, str):
        return redact_secrets(value)
    return value


__all__: Iterable[str] = ("redact_secrets", "structlog_redaction_processor")
