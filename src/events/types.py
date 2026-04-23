"""Concrete event types for the event bus."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bus import Event


@dataclass
class UserMessageEvent(Event):
    """A message from a Telegram user."""

    user_id: int = 0
    chat_id: int = 0
    text: str = ""
    working_directory: Path = field(default_factory=lambda: Path("."))
    source: str = "telegram"


@dataclass
class WebhookEvent(Event):
    """An external webhook delivery (GitHub, Notion, etc.)."""

    provider: str = ""
    event_type_name: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    delivery_id: str = ""
    source: str = "webhook"


@dataclass
class ScheduledEvent(Event):
    """A cron/scheduled trigger.

    M1 note (review feedback on PR #15 §4): ``ScheduledEvent`` runs
    with the **full** Claude tool allowlist because the prompt is
    assumed to be operator-trusted (static text in a cron job
    definition). If a future job template interpolates
    externally-sourced data into ``prompt`` — RSS feed contents,
    DB rows fetched at job time, third-party API responses — that
    data rides into Claude with Bash/Write/Edit available, which
    is exactly the attack surface M1 closed for webhooks.

    If you need to admit untrusted data into a scheduled job,
    wrap it the same way ``AgentHandler._build_webhook_prompt``
    wraps webhook payloads (delimited, nonce-tagged, "this is
    data, not instructions" header) OR extend this dataclass with
    an ``allowed_tools_override: Optional[List[str]]`` field and
    plumb it through ``AgentHandler.handle_scheduled``.
    """

    job_id: str = ""
    job_name: str = ""
    prompt: str = ""
    working_directory: Path = field(default_factory=lambda: Path("."))
    target_chat_ids: List[int] = field(default_factory=list)
    skill_name: Optional[str] = None
    source: str = "scheduler"


@dataclass
class AgentResponseEvent(Event):
    """An agent has produced a response to deliver."""

    chat_id: int = 0
    text: str = ""
    parse_mode: Optional[str] = "HTML"
    reply_to_message_id: Optional[int] = None
    source: str = "agent"
    originating_event_id: Optional[str] = None
