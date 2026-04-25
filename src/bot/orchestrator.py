"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..observability import bot_metrics
from ..projects import PrivateTopicsUnavailableError

# Secret-redaction patterns live in ``src.utils.redaction`` so the
# SDK integration (and any future log sink) can apply the same rules
# — keeping the backwards-compatible private name here for code
# outside this module that imports ``_redact_secrets`` directly.
from ..utils.redaction import redact_secrets as _redact_secrets
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

logger = structlog.get_logger()

_MEDIA_TYPE_MAP = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


@dataclass
class ActiveRequest:
    """Tracks an in-flight Claude request so it can be interrupted."""

    user_id: int
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._active_requests: Dict[int, ActiveRequest] = {}
        self._known_commands: frozenset[str] = frozenset()

    def interrupt_all_active_requests(self) -> int:
        """Signal every in-flight Claude request to stop (R4).

        The per-request ``asyncio.Event`` is wired into
        ``execute_command`` via a watcher task that calls
        ``client.interrupt()`` on the Claude SDK when the event is
        set — so simply setting the event here triggers a clean
        user-facing "interrupted by shutdown" exit on the next
        turn boundary without needing shared state across the
        shutdown path.

        Called from ``main.py`` during graceful shutdown **before**
        we stop the PTB ``Application`` — giving in-flight handlers
        a chance to wind down instead of leaving orphaned Claude
        subprocesses whose Stop buttons point at a dead process.

        Returns the number of active requests that were signalled.
        """
        signalled = 0
        for req in list(self._active_requests.values()):
            if not req.interrupt_event.is_set():
                req.interrupt_event.set()
                req.interrupted = True
                signalled += 1
        if signalled:
            logger.info(
                "Signalled in-flight requests to interrupt on shutdown",
                count=signalled,
            )
        return signalled

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                # query.answer expires after ~1 minute — failure is harmless.
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("fresh", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("repository", self.agentic_repo),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        # Derive known commands dynamically — avoids drift when new commands are added
        self._known_commands: frozenset[str] = frozenset(cmd for cmd, _ in handlers)

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Claude (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0
        # (higher priority). This catches any /command not matched there
        # and forwards it to Claude, while skipping known commands to
        # avoid double-firing.
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Stop button callback (must be before cd: handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        # /verbose inline keyboard callback
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_verbose_callback),
                pattern=r"^verbose:",
            )
        )

        # /repository "Ok" confirm-and-dismiss button
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_repo_ok),
                pattern=r"^repo_ok$",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("fresh", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("verbose", "Set output verbosity"),
                BotCommand("repository", "Switch workspace"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /fresh (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Detailed status: model, limits, session, cost."""
        # Force a fresh probe of Anthropic rate limits so /status reflects the
        # current 5h/7d windows even if the throttle would otherwise skip it.
        try:
            await self._refresh_anthropic_limits(
                context.bot_data,
                model=context.user_data.get("last_claude_model"),
                force=True,
            )
        except Exception:
            logger.debug("agentic_status: refresh limits failed", exc_info=True)

        model_display = (
            context.user_data.get("last_claude_model")
            or self.settings.claude_model
            or "(send a message first)"
        )
        max_cost_user = self.settings.claude_max_cost_per_user
        max_cost_req = self.settings.claude_max_cost_per_request

        # Cost info
        current_cost = 0.0
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
            except Exception:
                # Rate limiter shape varies; fall back to 0 cost.
                logger.debug("rate_limiter.get_user_status failed", exc_info=True)

        cost_pct = (current_cost / max_cost_user * 100) if max_cost_user > 0 else 0
        cost_bar = "█" * int(cost_pct / 10) + "░" * (10 - int(cost_pct / 10))

        # Anthropic rate limits — read from cache saved after last Claude response
        from datetime import datetime
        from datetime import timezone as _tz

        def _fmt_reset(ts: str) -> str:
            try:
                _dt = datetime.fromtimestamp(int(ts), tz=_tz.utc)
                _secs = int((_dt - datetime.now(_tz.utc)).total_seconds())
                if _secs <= 0:
                    return "now"
                h, m = divmod(_secs // 60, 60)
                return f"{h}h {m}m" if h else f"{m}m"
            except Exception:
                return ts

        # /status output: flat fields (Account, Model, Plan, Workspace).
        # Section headers are intentionally omitted.
        _account = context.bot_data.get("anthropic_account_info", {}) or {}
        _plan = _account.get("plan")
        _rl_tier = _account.get("rate_limit_tier")
        _email = _account.get("email")
        _plan_upper = _plan.upper() if _plan else None
        _plan_display = (
            f"{_plan_upper}({_rl_tier})"
            if _plan_upper and _rl_tier
            else (_plan_upper or _rl_tier)
        )

        _cached = context.bot_data.get("anthropic_rate_limits", {})
        _cached_model = (
            _cached.get("model")
            if isinstance(_cached, dict) and _cached.get("model")
            else model_display
        )

        # Workspace: relative-to-base when below it, else absolute.
        _base = self.settings.approved_directory
        _cur_dir = context.user_data.get("current_directory", _base)
        try:
            _rel = _cur_dir.resolve().relative_to(_base.resolve())
            _workspace_display = "/" if str(_rel) == "." else f"/{_rel.as_posix()}"
        except (ValueError, OSError):
            _workspace_display = str(_cur_dir)

        # Cost block — only relevant when using direct API key
        using_api_key = bool(self.settings.anthropic_api_key)
        cost_lines = []
        if using_api_key:
            cost_lines = [
                "💰 <b>Cost</b>",
                f"  Used: <code>${current_cost:.4f}</code> / "
                f"<code>${max_cost_user:.2f}</code>  [{cost_bar}] {cost_pct:.1f}%",
                f"  Per request: <code>${max_cost_req:.2f}</code>",
                "",
            ]

        lines: List[str] = [*cost_lines]
        if _email:
            lines.append(f"Account: <code>{_email}</code>")
        lines.append(f"Model: <code>{_cached_model}</code>")
        if _plan_display:
            lines.append(f"Plan: <code>{_plan_display}</code>")
        lines.append(f"Workspace: <code>{escape_html(_workspace_display)}</code>")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    @staticmethod
    def _decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
        """Best-effort decode of a JWT's payload (no signature verification).

        Returns None when the token isn't a JWT or fails to decode. Used to
        surface email/plan claims for the /status display.
        """
        import base64
        import json as _json

        parts = token.split(".")
        if len(parts) < 2:
            return None
        try:
            payload_b64 = parts[1]
            # base64url -> base64 with proper padding
            padding = "=" * (-len(payload_b64) % 4)
            decoded = base64.urlsafe_b64decode(payload_b64 + padding)
            data = _json.loads(decoded.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def _refresh_anthropic_limits(
        self,
        bot_data: Dict[str, Any],
        model: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """Fetch Anthropic unified rate-limit headers and cache them in bot_data.

        Throttled to at most once every 5 minutes (use ``force=True`` to bypass,
        e.g. when the user explicitly asked via /status). Uses the OAuth access
        token from ~/.claude/.credentials.json (same auth the Claude CLI uses).
        Makes a minimal 1-token API call just to read the response headers.
        Errors are silently swallowed — rate-limits display is best-effort.
        """
        import json as _json
        import time as _time

        import httpx

        _REFRESH_INTERVAL = 300  # 5 minutes

        now = _time.monotonic()
        last = bot_data.get("anthropic_limits_last_refresh", 0.0)
        if not force and now - last < _REFRESH_INTERVAL:
            return

        # Mark refresh time immediately to avoid parallel refreshes
        bot_data["anthropic_limits_last_refresh"] = now

        try:
            # Read OAuth token from Claude CLI credentials file
            creds_path = Path.home() / ".claude" / ".credentials.json"
            if not creds_path.exists():
                return
            creds = _json.loads(creds_path.read_text())
            oauth_blob = creds.get("claudeAiOauth") or {}
            token = (
                oauth_blob.get("accessToken")
                or creds.get("oauthToken")
                or creds.get("access_token")
            )
            if not token:
                return

            # Cache account/plan info derived from credentials + JWT claims.
            # OAuth blob commonly carries {scopes, subscriptionType, ...};
            # JWT payload usually carries {email, sub, ...}. Both are best-effort.
            account_info: Dict[str, Any] = bot_data.get(
                "anthropic_account_info", {}
            ).copy() if isinstance(
                bot_data.get("anthropic_account_info"), dict
            ) else {}
            # Diagnostic: log the *keys* present (no secret values) so we can
            # see what's actually in this user's credentials when /status
            # doesn't surface Plan/Account.
            try:
                _claims_for_log = self._decode_jwt_payload(token) or {}
            except Exception:
                _claims_for_log = {}
            _SAFE_VALUE_KEYS = {
                "email",
                "user_email",
                "subscription_type",
                "subscriptionType",
                "plan",
                "tier",
                "rateLimitTier",
                "scopes",
                "scope",
                "organization_name",
                "organization",
                "org_name",
            }
            logger.info(
                "Anthropic credentials keys",
                top_keys=list(creds.keys()),
                oauth_keys=list(oauth_blob.keys()),
                oauth_safe_values={
                    k: v
                    for k, v in oauth_blob.items()
                    if k in _SAFE_VALUE_KEYS
                },
                jwt_keys=list(_claims_for_log.keys())
                if isinstance(_claims_for_log, dict)
                else "not_jwt",
                jwt_safe_values={
                    k: v
                    for k, v in (_claims_for_log or {}).items()
                    if k in _SAFE_VALUE_KEYS
                }
                if isinstance(_claims_for_log, dict)
                else {},
            )
            for key in ("subscriptionType", "subscription_type", "tier", "plan"):
                v = oauth_blob.get(key)
                if v:
                    account_info["plan"] = str(v)
                    break
            _rl_tier = oauth_blob.get("rateLimitTier")
            if _rl_tier:
                account_info["rate_limit_tier"] = str(_rl_tier)
            for key in ("emailAddress", "email"):
                v = oauth_blob.get(key)
                if v:
                    account_info["email"] = str(v)
                    break
            try:
                _claims = self._decode_jwt_payload(token)
                if isinstance(_claims, dict):
                    if "email" not in account_info:
                        for key in ("email", "user_email"):
                            v = _claims.get(key)
                            if v:
                                account_info["email"] = str(v)
                                break
                    if "plan" not in account_info:
                        for key in (
                            "subscription_type",
                            "subscriptionType",
                            "plan",
                            "tier",
                        ):
                            v = _claims.get(key)
                            if v:
                                account_info["plan"] = str(v)
                                break
                    if "org" not in account_info:
                        for key in (
                            "organization_name",
                            "organization",
                            "org_name",
                        ):
                            v = _claims.get(key)
                            if v:
                                account_info["org"] = str(v)
                                break
            except Exception:
                pass

            req_headers = {
                "anthropic-version": "2023-06-01",
                "Authorization": f"Bearer {token}",
                "content-type": "application/json",
            }

            # Try OAuth profile endpoint(s) for email — we have user:profile
            # scope. Endpoints aren't formally documented; try a few known ones.
            if "email" not in account_info:
                _profile_endpoints = [
                    "https://api.anthropic.com/api/oauth/profile",
                    "https://api.anthropic.com/api/oauth/userinfo",
                    "https://api.anthropic.com/api/account",
                    "https://api.anthropic.com/api/me",
                ]
                async with httpx.AsyncClient(timeout=10) as _pc:
                    for _url in _profile_endpoints:
                        try:
                            _r = await _pc.get(_url, headers=req_headers)
                        except Exception as _pe:
                            logger.info(
                                "Profile endpoint network error",
                                url=_url,
                                error=str(_pe),
                            )
                            continue
                        logger.info(
                            "Profile endpoint response",
                            url=_url,
                            status=_r.status_code,
                            body_preview=_r.text[:300] if _r.status_code != 200 else "<200 ok>",
                        )
                        if _r.status_code != 200:
                            continue
                        try:
                            _data = _r.json()
                        except Exception:
                            continue
                        if not isinstance(_data, dict):
                            continue
                        # Walk common shapes for an email field.
                        _candidates = [_data]
                        for _nest_key in ("user", "account", "profile", "data"):
                            _nest = _data.get(_nest_key)
                            if isinstance(_nest, dict):
                                _candidates.append(_nest)
                        for _obj in _candidates:
                            for _ek in ("email", "emailAddress", "user_email"):
                                _v = _obj.get(_ek)
                                if _v:
                                    account_info["email"] = str(_v)
                                    break
                            if "email" in account_info:
                                break
                        if "email" in account_info:
                            break

            if account_info:
                bot_data["anthropic_account_info"] = account_info

            payload = {
                "model": model or "claude-sonnet-4-5",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers=req_headers,
                )
                h = resp.headers
                cache: Dict[str, Any] = {}
                if "anthropic-ratelimit-unified-5h-utilization" in h:
                    cache["5h_util"] = float(
                        h["anthropic-ratelimit-unified-5h-utilization"]
                    )
                if "anthropic-ratelimit-unified-7d-utilization" in h:
                    cache["7d_util"] = float(
                        h["anthropic-ratelimit-unified-7d-utilization"]
                    )
                if "anthropic-ratelimit-unified-5h-reset" in h:
                    cache["5h_reset"] = h["anthropic-ratelimit-unified-5h-reset"]
                if "anthropic-ratelimit-unified-7d-reset" in h:
                    cache["7d_reset"] = h["anthropic-ratelimit-unified-7d-reset"]
                # Only stamp the model when we actually captured rate-limit
                # data — otherwise the cache becomes truthy with model-only
                # contents and /status falsely renders 0%/0%.
                if cache and model:
                    cache["model"] = model
                # Log every anthropic-* header value so we can diagnose when
                # the displayed limits look wrong (0%/0% etc.).
                _anthropic_headers = {
                    k: v for k, v in h.items() if k.lower().startswith("anthropic-")
                }
                logger.info(
                    "Anthropic probe response",
                    status=resp.status_code,
                    anthropic_headers=_anthropic_headers,
                )

                # Only update cache if we got at least one header
                if cache:
                    bot_data["anthropic_rate_limits"] = cache
                    bot_data.pop("anthropic_limits_last_error", None)
                else:
                    bot_data["anthropic_limits_last_error"] = (
                        f"HTTP {resp.status_code}: no rate-limit headers"
                    )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            bot_data["anthropic_limits_last_error"] = err
            logger.warning("Failed to refresh Anthropic rate limits", error=err)
            # Reset last refresh so next call tries again sooner
            bot_data["anthropic_limits_last_refresh"] = 0.0

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2] or pick from inline buttons."""
        args = update.message.text.split()[1:] if update.message.text else []
        labels = {0: "quiet", 1: "normal", 2: "detailed"}

        if not args:
            current = self._get_verbose_level(context)

            def _btn_label(level: int) -> str:
                # Mark the current selection so users see which one is active.
                base = labels[level]
                return f"• {base} •" if level == current else base

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            _btn_label(0), callback_data="verbose:0"
                        ),
                        InlineKeyboardButton(
                            _btn_label(1), callback_data="verbose:1"
                        ),
                        InlineKeyboardButton(
                            _btn_label(2), callback_data="verbose:2"
                        ),
                    ]
                ]
            )
            await update.message.reply_text(
                f"Verbosity: <b>{labels.get(current, '?')}</b>\n"
                "Choose a level:\n"
                "  quiet — final response only\n"
                "  normal — tools + reasoning\n"
                "  detailed — tools with inputs + reasoning",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        await update.message.reply_text(
            f"Verbosity set to <b>{labels[level]}</b>",
            parse_mode="HTML",
        )

    async def _handle_verbose_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Apply the level chosen from /verbose's inline keyboard."""
        query = update.callback_query
        if query is None or not query.data:
            return
        try:
            level = int(query.data.split(":", 1)[1])
        except (IndexError, ValueError):
            await query.answer("Invalid selection", show_alert=False)
            return
        if level not in (0, 1, 2):
            await query.answer("Invalid level", show_alert=False)
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await query.answer(f"Verbosity: {labels[level]}", show_alert=False)
        try:
            await query.edit_message_text(
                f"Verbosity set to <b>{labels[level]}</b>",
                parse_mode="HTML",
            )
        except Exception:
            # Editing may fail if the original message is gone — fall back
            # to a fresh reply rather than letting the handler crash.
            if update.effective_chat:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"Verbosity set to <b>{labels[level]}</b>",
                    parse_mode="HTML",
                )

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        # Telegram network blips shouldn't kill the heartbeat.
                        pass
            except asyncio.CancelledError:
                # Expected: caller cancels the heartbeat when done.
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = mcp_images is not None and approved_directory is not None

        if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return

            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        )
                    except Exception:
                        # edit_text fails on identical content / rate
                        # limits / message-too-old; progress UI is
                        # best-effort, drop and keep streaming.
                        pass

        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    for idx, img in enumerate(photos[:10]):
                        media.append(
                            InputMediaPhoto(
                                media=img.path.read_bytes(),
                                caption=(caption if use_caption and idx == 0 else None),
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    await update.message.chat.send_media_group(
                        media=media,
                        reply_to_message_id=reply_to_message_id,
                    )
                    caption_sent = use_caption
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # R5 — bump the received-messages counter before the rate-limit
        # gate so we can see the full inbound volume in /metrics and
        # compare it against the ``rate_limit_rejections`` counter
        # below to spot noisy users quickly.
        await bot_metrics.messages_received_total.inc()

        # Rate limit + budget pre-check. The ``reserve_request`` path
        # uses the real per-request ceiling (``claude_max_cost_per_request``)
        # as the worst-case so we reject if the user is one big call away
        # from blowing their daily cap. The actual billed cost is recorded
        # via ``track_actual_cost`` after the response lands.
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            worst_case = self.settings.claude_max_cost_per_request
            allowed, limit_message = await rate_limiter.reserve_request(
                user_id, worst_case_cost=worst_case
            )
            if not allowed:
                await bot_metrics.rate_limit_rejections_total.inc(reason="rate")
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)

        # Create Stop button and interrupt event
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )
        progress_msg = await update.message.reply_text(
            "Working...", reply_markup=stop_kb
        )

        # Register active request for stop callback
        active_request = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )
        self._active_requests[user_id] = active_request

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            self._active_requests.pop(user_id, None)
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration.",
                reply_markup=None,
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            reply_markup=stop_kb,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            interrupt_event=interrupt_event,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        # R5 — snapshot monotonic time around the Claude call so the
        # ``bot_claude_latency_seconds`` histogram reflects wall-clock
        # latency regardless of branch taken inside the try/except.
        claude_call_started = time.monotonic()
        claude_outcome = "error"  # overwritten on success / interrupt

        success = True
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id
            if claude_response.model:
                context.user_data["last_claude_model"] = claude_response.model

            # Refresh Anthropic rate-limit cache (throttled, fire-and-forget)
            asyncio.create_task(
                self._refresh_anthropic_limits(
                    context.bot_data,
                    model=claude_response.model,
                )
            )

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Record the real billed cost in the in-memory rate
            # limiter so the next ``reserve_request`` check has accurate
            # numbers. The DB is already updated via
            # ``save_claude_interaction`` above.
            if rate_limiter and claude_response.cost:
                try:
                    await rate_limiter.track_actual_cost(user_id, claude_response.cost)
                except Exception as e:
                    logger.warning(
                        "Failed to track actual cost in rate limiter",
                        user_id=user_id,
                        error=str(e),
                    )

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)

            response_content = claude_response.content
            if claude_response.interrupted:
                response_content = (
                    response_content or ""
                ) + "\n\n_(Interrupted by user)_"

            formatted_messages = formatter.format_claude_response(response_content)
            claude_outcome = "interrupted" if claude_response.interrupted else "success"

        except Exception as e:
            success = False
            claude_outcome = "error"
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            # Record metrics regardless of branch taken. Wrapped in a
            # try so a metrics failure cannot block cleanup.
            try:
                latency = time.monotonic() - claude_call_started
                await bot_metrics.claude_calls_total.inc(outcome=claude_outcome)
                await bot_metrics.claude_latency_seconds.observe(latency)
            except Exception as metrics_err:
                logger.debug(
                    "Failed to record Claude metrics",
                    error=str(metrics_err),
                )
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()

            # M6 — now that we have the bytes, run the magic-byte
            # validation. Executable uploads and extension/content
            # mismatches get rejected here with an audit trail; a
            # security_validator is required for this check, which
            # matches the earlier filename-only validation gate
            # higher up in this method.
            audit_logger = context.bot_data.get("audit_logger")
            if security_validator is not None:
                from ..bot.middleware.security import validate_file_upload

                ok, err = await validate_file_upload(
                    document,
                    security_validator,
                    user_id,
                    audit_logger,
                    file_bytes=bytes(file_bytes),
                )
                if not ok:
                    await progress_msg.edit_text(f"File rejected: {err}")
                    return

            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_doc: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_doc,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
            )

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_doc

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            fmt = processed_image.metadata.get("format", "png")
            images = [
                {
                    "data": processed_image.base64_data,
                    "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                }
            ]

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Transcribing...")

        try:
            voice = update.message.voice
            processed_voice = await voice_handler.process_voice_message(
                voice, update.message.caption
            )

            await progress_msg.edit_text("Working...")
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Run a media-derived prompt through Claude and send responses."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
            )
        finally:
            heartbeat.cancel()

        if force_new:
            context.user_data["force_new_session"] = False

        context.user_data["claude_session_id"] = claude_response.session_id

        from .handlers.message import _update_working_directory_from_claude_response

        _update_working_directory_from_claude_response(
            claude_response, context, self.settings, user_id
        )

        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        formatted_messages = formatter.format_claude_response(claude_response.content)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls).
        images: List[ImageAttachment] = mcp_images_media

        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Claude in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but
        returns immediately when the command is registered, preventing
        double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Claude as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        if self.settings.voice_provider == "local":
            return (
                "Voice processing is not available. "
                "Ensure whisper.cpp is installed and the model file exists. "
                "Check WHISPER_CPP_BINARY_PATH and WHISPER_CPP_MODEL_PATH settings."
            )
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show a directory browser rooted at the current workspace.

        /repository          — show current dir's subfolders + ".." up
        /repository <name>   — switch to a subfolder of the current directory
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if args:
            target_name = args[0]
            target_path = (current_dir / target_name).resolve()
            if not self._path_within(target_path, base) or not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return
            session_id = await self._switch_workspace(
                context, update.effective_user.id, target_path
            )
            await update.message.reply_text(
                self._workspace_switch_text(base, target_path, session_id),
                parse_mode="HTML",
            )
            return

        text, markup = self._build_dir_browser(current_dir, base)
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=markup
        )

    @staticmethod
    def _path_within(path: Path, root: Path) -> bool:
        """Return True if ``path`` is ``root`` or below it."""
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _workspace_switch_text(
        base: Path, target: Path, session_id: Optional[str]
    ) -> str:
        try:
            rel = target.resolve().relative_to(base.resolve())
            display = "/" if str(rel) == "." else f"/{rel.as_posix()}"
        except ValueError:
            display = str(target)
        is_git = (target / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " (session resumed)" if session_id else ""
        return (
            f"Switched to <code>{escape_html(display)}</code>"
            f"{git_badge}{session_badge}"
        )

    async def _switch_workspace(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        target_path: Path,
    ) -> Optional[str]:
        """Update current_directory and resume a session if available."""
        context.user_data["current_directory"] = target_path
        claude_integration = context.bot_data.get("claude_integration")
        session_id: Optional[str] = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                user_id, target_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id
        return session_id

    def _build_dir_browser(
        self, current_dir: Path, base: Path
    ) -> "tuple[str, InlineKeyboardMarkup]":
        """Render the listing text and inline keyboard for ``current_dir``."""
        try:
            entries = sorted(
                [
                    d
                    for d in current_dir.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name.lower(),
            )
            error_line = ""
        except OSError as e:
            entries = []
            error_line = f"\n<i>Error reading directory: {escape_html(str(e))}</i>"

        try:
            rel = current_dir.resolve().relative_to(base.resolve())
            header_path = "/" if str(rel) == "." else f"/{rel.as_posix()}"
        except ValueError:
            header_path = str(current_dir)

        lines: List[str] = [
            f"<b>Workspace:</b> <code>{escape_html(header_path)}</code>"
        ]
        if entries:
            lines.append("")
            for d in entries:
                is_git = (d / ".git").is_dir()
                icon = "\U0001f4e6" if is_git else "\U0001f4c1"
                lines.append(f"{icon} <code>{escape_html(d.name)}/</code>")
        else:
            lines.append("<i>(no subfolders)</i>")
        if error_line:
            lines.append(error_line)

        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        try:
            at_root = current_dir.resolve() == base.resolve()
        except OSError:
            at_root = False
        # Top row: ".." (when not at root) + "Ok" to confirm current workspace
        # and dismiss the browser without further navigation.
        top_row: List[InlineKeyboardButton] = []
        if not at_root and self._path_within(current_dir.parent, base):
            top_row.append(InlineKeyboardButton("⬆ ..", callback_data="cd:.."))
        top_row.append(InlineKeyboardButton("✅ Ok", callback_data="repo_ok"))
        keyboard_rows.append(top_row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    safe = name[:55]
                    row.append(
                        InlineKeyboardButton(
                            f"\U0001f4c1 {name}", callback_data=f"cd:{safe}"
                        )
                    )
            keyboard_rows.append(row)

        return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request."""
        query = update.callback_query
        target_user_id = int(query.data.split(":", 1)[1])

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer(
                "Only the requesting user can stop this.", show_alert=True
            )
            return

        active = self._active_requests.get(target_user_id)
        if not active:
            await query.answer("Already completed.", show_alert=False)
            return
        if active.interrupted:
            await query.answer("Already stopping...", show_alert=False)
            return

        active.interrupt_event.set()
        active.interrupted = True
        await query.answer("Stopping...", show_alert=False)

        try:
            await active.progress_msg.edit_text("Stopping...", reply_markup=None)
        except Exception:
            # Best-effort UI hint; ignore Telegram edit failures.
            pass

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks for the directory browser.

        Resolves the target relative to the user's current workspace, switches
        to it, and re-renders the browser in place so users can keep
        navigating. ``cd:..`` walks up one level (clamped to the approved root).
        """
        query = update.callback_query
        await query.answer()

        data = query.data
        _, name = data.split(":", 1)

        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if name == "..":
            new_path = current_dir.parent
        else:
            new_path = current_dir / name
        new_path = new_path.resolve()

        if not self._path_within(new_path, base) or not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(name)}</code>",
                parse_mode="HTML",
            )
            return

        session_id = await self._switch_workspace(
            context, query.from_user.id, new_path
        )

        # Re-render the browser at the new directory so the user can keep
        # navigating without re-running /repository. The browser header
        # already shows the active workspace, so no separate "Switched to"
        # line is needed.
        del session_id  # workspace is switched; result is reflected in header
        text, markup = self._build_dir_browser(new_path, base)
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )

        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[name],
                success=True,
            )

    async def _handle_repo_ok(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Confirm the current workspace and dismiss the directory browser."""
        query = update.callback_query
        await query.answer("OK")

        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)
        try:
            rel = current_dir.resolve().relative_to(base.resolve())
            display = "/" if str(rel) == "." else f"/{rel.as_posix()}"
        except (ValueError, OSError):
            display = str(current_dir)

        try:
            await query.edit_message_text(
                f"Workspace: <code>{escape_html(display)}</code>",
                parse_mode="HTML",
            )
        except Exception:
            # Best-effort dismiss; ignore Telegram edit failures.
            pass
