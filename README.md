# Claude Code Telegram Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

A Telegram bot that gives you remote access to [Claude Code](https://claude.ai/code). Chat naturally with Claude about your projects from anywhere -- no terminal commands needed.

## What is this?

This bot connects Telegram to Claude Code, providing a conversational AI interface for your codebase:

- **Chat naturally** -- ask Claude to analyze, edit, or explain your code in plain language
- **Maintain context** across conversations with automatic session persistence per project
- **Code on the go** from any device with Telegram
- **Receive proactive notifications** from webhooks, scheduled jobs, and CI/CD events
- **Stay secure** with built-in authentication, directory sandboxing, and audit logging

## Quick Start

### Demo

```
You: Can you help me add error handling to src/api.py?

Bot: I'll analyze src/api.py and add error handling...
     [Claude reads your code, suggests improvements, and can apply changes directly]

You: Looks good. Now run the tests to make sure nothing broke.

Bot: Running pytest...
     All 47 tests passed. The error handling changes are working correctly.
```

### 1. Prerequisites

- **Python 3.11+** -- [Download here](https://www.python.org/downloads/)
- **Claude Code CLI** -- [Install from here](https://claude.ai/code)
- **Telegram Bot Token** -- Get one from [@BotFather](https://t.me/botfather)

### 2. Install

Choose your preferred method:

#### Option A: Install from a release tag (Recommended)

```bash
# Using uv (recommended — installs in an isolated environment)
uv tool install git+https://github.com/RichardAtCT/claude-code-telegram@v1.3.0

# Or using pip
pip install git+https://github.com/RichardAtCT/claude-code-telegram@v1.3.0

# Track the latest stable release
pip install git+https://github.com/RichardAtCT/claude-code-telegram@latest
```

#### Option B: From source (for development)

```bash
git clone https://github.com/RichardAtCT/claude-code-telegram.git
cd claude-code-telegram
make dev  # requires Poetry
```

> **Note:** Always install from a tagged release (not `main`) for stability. See [Releases](https://github.com/RichardAtCT/claude-code-telegram/releases) for available versions.

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your settings:
```

**Minimum required:**
```bash
TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_BOT_USERNAME=my_claude_bot
APPROVED_DIRECTORY=/Users/yourname/projects
ALLOWED_USERS=123456789  # Your Telegram user ID
```

### 4. Run

```bash
make run          # Production
make run-debug    # With debug logging
```

Message your bot on Telegram to get started.

> **Detailed setup:** See [docs/setup.md](docs/setup.md) for Claude authentication options and troubleshooting.

## Modes

The bot supports two interaction modes:

### Agentic Mode (Default)

The default conversational mode. Just talk to Claude naturally -- no special commands required.

**Commands:** `/start`, `/new`, `/status`, `/verbose`, `/repo`
If `ENABLE_PROJECT_THREADS=true`: `/sync_threads`

```
You: What files are in this project?
Bot: Working... (3s)
     📖 Read
     📂 LS
     💬 Let me describe the project structure
Bot: [Claude describes the project structure]

You: Add a retry decorator to the HTTP client
Bot: Working... (8s)
     📖 Read: http_client.py
     💬 I'll add a retry decorator with exponential backoff
     ✏️ Edit: http_client.py
     💻 Bash: poetry run pytest tests/ -v
Bot: [Claude shows the changes and test results]

You: /verbose 0
Bot: Verbosity set to 0 (quiet)
```

Use `/verbose 0|1|2` to control how much background activity is shown:

| Level | Shows |
|-------|-------|
| **0** (quiet) | Final response only (typing indicator stays active) |
| **1** (normal, default) | Tool names + reasoning snippets in real-time |
| **2** (detailed) | Tool names with inputs + longer reasoning text |

#### GitHub Workflow

Claude Code already knows how to use `gh` CLI and `git`. Authenticate on your server with `gh auth login`, then work with repos conversationally:

```
You: List my repos related to monitoring
Bot: [Claude runs gh repo list, shows results]

You: Clone the uptime one
Bot: [Claude runs gh repo clone, clones into workspace]

You: /repo
Bot: 📦 uptime-monitor/  ◀
     📁 other-project/

You: Show me the open issues
Bot: [Claude runs gh issue list]

You: Create a fix branch and push it
Bot: [Claude creates branch, commits, pushes]
```

Use `/repo` to list cloned repos in your workspace, or `/repo <name>` to switch directories (sessions auto-resume).

### Classic Mode

Set `AGENTIC_MODE=false` to enable the full 13-command terminal-like interface with directory navigation, inline keyboards, quick actions, git integration, and session export.

**Commands:** `/start`, `/help`, `/new`, `/continue`, `/end`, `/status`, `/cd`, `/ls`, `/pwd`, `/projects`, `/export`, `/actions`, `/git`  
If `ENABLE_PROJECT_THREADS=true`: `/sync_threads`

```
You: /cd my-web-app
Bot: Directory changed to my-web-app/

You: /ls
Bot: src/  tests/  package.json  README.md

You: /actions
Bot: [Run Tests] [Install Deps] [Format Code] [Run Linter]
```

## Event-Driven Automation

Beyond direct chat, the bot can respond to external triggers:

- **Webhooks** -- Receive GitHub events (push, PR, issues) and route them through Claude for automated summaries or code review
- **Scheduler** -- Run recurring Claude tasks on a cron schedule (e.g., daily code health checks)
- **Notifications** -- Deliver agent responses to configured Telegram chats
- **Metrics** -- Scrape `/metrics` on the webhook API server for Prometheus-format counters and histograms (inbound messages, rate-limit rejections, DB query latency, active sessions)

Enable with `ENABLE_API_SERVER=true` and `ENABLE_SCHEDULER=true`. The API server binds to `127.0.0.1` by default; set `API_SERVER_HOST=0.0.0.0` only if you are deliberately exposing the webhook endpoint. See [docs/setup.md](docs/setup.md) for configuration.

## Features

### Working Features

- Conversational agentic mode (default) with natural language interaction
- Classic terminal-like mode with 13 commands and inline keyboards
- Full Claude Code integration with SDK (primary) and CLI (fallback)
- Automatic session persistence per user/project directory
- Multi-layer authentication (whitelist + HMAC-SHA256 token auth)
- Per-user concurrency lock -- long-running requests never block other users
- Rate limiting with token bucket algorithm and real cost tracking
- Directory sandboxing with path traversal prevention
- File upload handling with magic-byte validation and archive extraction
- Image/screenshot upload with analysis
- Voice message transcription (Mistral Voxtral / OpenAI Whisper / [local whisper.cpp](docs/local-whisper-cpp.md))
- Git integration with safe repository operations
- Quick actions system with context-aware buttons
- Session export in Markdown, HTML, and JSON formats
- SQLite persistence with WAL mode, idempotent migrations, and BEGIN IMMEDIATE transactions
- Usage and cost tracking with per-request budget reservation
- Durable audit logging (SQLite) with optional append-only JSONL forensic sink
- Event bus for decoupled message routing
- Webhook API server (GitHub HMAC-SHA256, generic Bearer token auth, localhost-bound by default)
- Job scheduler with cron expressions and persistent storage
- Notification service with per-chat rate limiting
- Graceful shutdown that interrupts in-flight Claude calls cleanly
- Optional PTB state persistence (directories, session IDs, verbose level) across restarts

- Tunable verbose output showing Claude's tool usage and reasoning in real-time
- Persistent typing indicator so users always know the bot is working
- 16 configurable tools with allowlist/disallowlist control (see [docs/tools.md](docs/tools.md))

### Observability

- **Prometheus `/metrics` endpoint** -- served from the webhook API server in the standard Prometheus text format. No external `prometheus_client` dependency.
- **Correlation IDs** -- every inbound update gets a request ID (and user ID when known) bound to `structlog` via `contextvars`. Every downstream log line (middleware, handlers, storage, Claude SDK) carries the same IDs, so a single grep stitches the full request timeline together.
- **Hot-path instrumentation** -- received-message counter, rate-limit rejections, DB query duration histograms, active Claude session gauges.

### Planned Enhancements

- Plugin system for third-party extensions

## Configuration

### Required

```bash
TELEGRAM_BOT_TOKEN=...           # From @BotFather
TELEGRAM_BOT_USERNAME=...        # Your bot's username
APPROVED_DIRECTORY=...           # Base directory for project access
ALLOWED_USERS=123456789          # Comma-separated Telegram user IDs
```

### Common Options

```bash
# Claude
ANTHROPIC_API_KEY=sk-ant-...     # API key (optional if using CLI auth)
CLAUDE_MAX_COST_PER_USER=10.0    # Spending limit per user (USD)
CLAUDE_TIMEOUT_SECONDS=300       # Operation timeout

# Mode
AGENTIC_MODE=true                # Agentic (default) or classic mode
VERBOSE_LEVEL=1                  # 0=quiet, 1=normal (default), 2=detailed

# Rate Limiting
RATE_LIMIT_REQUESTS=10           # Requests per window
RATE_LIMIT_WINDOW=60             # Window in seconds

# Features (classic mode)
ENABLE_GIT_INTEGRATION=true
ENABLE_FILE_UPLOADS=true
ENABLE_QUICK_ACTIONS=true
```

### Agentic Platform

```bash
# Webhook API Server
ENABLE_API_SERVER=false          # Enable FastAPI webhook server
API_SERVER_HOST=127.0.0.1        # Bind host (localhost by default; set 0.0.0.0 only if exposing publicly)
API_SERVER_PORT=8080             # Server port

# Webhook Authentication
GITHUB_WEBHOOK_SECRET=...        # GitHub HMAC-SHA256 secret
WEBHOOK_API_SECRET=...           # Bearer token for generic providers

# Scheduler
ENABLE_SCHEDULER=false           # Enable cron job scheduler

# Notifications
NOTIFICATION_CHAT_IDS=123,456    # Default chat IDs for proactive notifications

# Observability
AUDIT_LOG_PATH=/var/log/claude-bot/audit.jsonl  # Optional append-only JSONL audit sink
PTB_PERSISTENCE_PATH=/var/lib/claude-bot/ptb-state.pickle  # Optional PTB state across restarts
```

### Project Threads Mode

```bash
# Enable strict topic routing by project
ENABLE_PROJECT_THREADS=true

# Mode: private (default) or group
PROJECT_THREADS_MODE=private

# YAML registry file (see config/projects.example.yaml)
PROJECTS_CONFIG_PATH=config/projects.yaml

# Required only when PROJECT_THREADS_MODE=group
PROJECT_THREADS_CHAT_ID=-1001234567890

# Minimum delay (seconds) between Telegram API calls during topic sync
# Set 0 to disable pacing
PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS=1.1
```

In strict mode, only `/start` and `/sync_threads` work outside mapped project topics.
In private mode, `/start` auto-syncs project topics for your private bot chat.
To use topics with your bot, enable them in BotFather:
`Bot Settings -> Threaded mode`.

> **Full reference:** See [docs/configuration.md](docs/configuration.md) and [`.env.example`](.env.example).

### Finding Your Telegram User ID

Message [@userinfobot](https://t.me/userinfobot) on Telegram -- it will reply with your user ID number.

## Troubleshooting

**Bot doesn't respond:**
- Check your `TELEGRAM_BOT_TOKEN` is correct
- Verify your user ID is in `ALLOWED_USERS`
- Ensure Claude Code CLI is installed and accessible
- Check bot logs with `make run-debug`

**Claude integration not working:**
- SDK mode (default): Check `claude auth status` or verify `ANTHROPIC_API_KEY`
- CLI mode: Verify `claude --version` and `claude auth status`
- Check `CLAUDE_ALLOWED_TOOLS` includes necessary tools (see [docs/tools.md](docs/tools.md) for the full reference)

**High usage costs:**
- Adjust `CLAUDE_MAX_COST_PER_USER` to set spending limits
- Monitor usage with `/status`
- Use shorter, more focused requests

## Security

This bot implements defense-in-depth security across the auth, storage, transport, and tooling layers:

### Access & Identity

- **Access control** -- whitelist-based user authentication plus optional token-based auth for headless access
- **HMAC-SHA256 token hashing** -- tokens are stored as HMAC-SHA256 hashes keyed by a server-side secret, verified with `hmac.compare_digest` (constant-time) so neither length-extension attacks nor timing side-channels apply
- **Persistent token storage** -- token hashes live in SQLite rather than RAM, so revocations and new issuances survive restarts
- **Pydantic `SecretStr` handling** -- secrets are never accidentally stringified into logs or hash inputs (regression-tested)

### Request Safety

- **Per-user concurrency lock** -- requests from the same user serialize; requests from different users run in parallel. A stuck Claude call for one user never freezes the bot for everyone else
- **Rate limiting with cost pre-reservation** -- the worst-case per-request cost is reserved up front and reconciled with the actual billed cost after the response, so a single big call cannot silently blow the daily budget
- **Input validation** -- blocks `..`, `;`, `&&`, `$()`, backticks, and other shell injection patterns
- **Directory sandboxing** -- approved-directory enforcement with path traversal prevention
- **Bash tool read-path hardening** -- read-only commands (grep, awk, sed, xxd, etc.) are path-checked against the approved directory; wrapper commands (env, nice, timeout, sudo) are peeled and re-validated; redirects, command substitution, and unquoted globs are rejected

### Storage & Forensics

- **Durable SQLite audit log** -- every auth attempt, command execution, file access, and security violation is persisted to the `audit_log` table (session ID and risk level folded into a namespaced `_meta` dict to prevent key collisions)
- **Optional append-only JSONL audit sink** -- set `AUDIT_LOG_PATH` to fan out every audit event to a `fsync`'d JSONL file with POSIX 640 permissions; ship it off-host with `logrotate` + a log forwarder for tamper-evident forensic durability
- **SQLite WAL mode + `BEGIN IMMEDIATE`** -- write transactions take an immediate reserved lock so concurrent UPSERTs (tokens, sessions, audit rows) never race
- **Idempotent migrations** -- schema and index creation is safe to re-run on existing databases
- **Secret redaction** -- API keys, tokens, and known secret patterns are scrubbed from both user-facing errors and structured logs before they are written

### Transport & Webhooks

- **API server binds to `127.0.0.1` by default** -- accidental public exposure requires an explicit `API_SERVER_HOST=0.0.0.0`
- **GitHub HMAC-SHA256 signature verification** for GitHub webhooks; generic Bearer token auth for other providers
- **Atomic deduplication** -- the `webhook_events` table rejects duplicate deliveries at write time
- **Webhook-originated Claude runs use a restricted tool set** -- no `WebFetch` or `WebSearch` (eliminates the "attacker planted a payload -> webhook fires -> Claude exfiltrates via search" chain)
- **Nonce-tagged payload envelopes** -- every webhook-driven prompt wraps untrusted payloads in a fresh nonce-suffixed tag (`<untrusted_payload_$(token_hex)>...`) so a payload cannot close the tag early and inject trusted instructions

### File Handling

- **Magic-byte file validation** -- uploads are identified by their actual file signature (ZIP, PNG, JPEG, PDF, etc.), not by the extension or the filename the client sent, wired into every download call site
- **Path boundaries for tool writes** -- the `ToolMonitor` validates Claude's file paths against the approved directory before executing Write/Edit/MultiEdit

### Process & Runtime

- **Graceful shutdown** -- SIGTERM / SIGINT fire a global interrupt event; in-flight Claude calls unwind cleanly rather than being force-killed mid-session
- **User-facing errors are scrubbed** -- stack traces and internal module paths never reach the user; a correlation ID points the operator at the matching log line
- **CI/CD supply-chain controls** -- CodeQL, pip-audit, and Dependabot run on every PR (see `.github/workflows/`)

### Escape Hatches (Trusted Environments Only)

- `DISABLE_SECURITY_PATTERNS=true` -- relaxes input validation (default `false`)
- `DISABLE_TOOL_VALIDATION=true` -- skips tool name allowlist checks (default `false`)

See [SECURITY.md](SECURITY.md) for the full threat model and disclosure policy.

## Development

```bash
make dev           # Install all dependencies
make test          # Run tests with coverage
make lint          # Black + isort + flake8 + mypy
make format        # Auto-format code
make run-debug     # Run with debug logging
make run-watch     # Run with auto-restart on code changes
```

### CI / Supply-Chain Checks

Every push and PR runs:

- **Unit tests** with coverage reporting
- **Lint gate** -- Black, isort, flake8, mypy strict
- **CodeQL** -- static security analysis for Python
- **pip-audit** -- dependency vulnerability scan against the locked Poetry environment
- **Dependabot** -- automated PRs for `pip`, `github-actions`, and `docker` ecosystems

The Dockerfile runs as a non-root user with a read-only root filesystem and a minimal capability set -- see [`Dockerfile`](Dockerfile) and the hardened systemd deployment notes in [`SYSTEMD_SETUP.md`](SYSTEMD_SETUP.md) for reference.

> **Full documentation:** See the [docs index](docs/README.md) for all guides and references.

### Version Management

The version is defined once in `pyproject.toml` and read at runtime via `importlib.metadata`. To cut a release:

```bash
make bump-patch    # 1.2.0 -> 1.2.1 (bug fixes)
make bump-minor    # 1.2.0 -> 1.3.0 (new features)
make bump-major    # 1.2.0 -> 2.0.0 (breaking changes)
```

Each command commits, tags, and pushes automatically, triggering CI tests and a GitHub Release with auto-generated notes.

### Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make changes with tests: `make test && make lint`
4. Submit a Pull Request

**Code standards:** Python 3.11+, Black formatting (88 chars), type hints required, pytest with >85% coverage.

## License

MIT License -- see [LICENSE](LICENSE).

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=RichardAtCT/claude-code-telegram&type=Date)](https://star-history.com/#RichardAtCT/claude-code-telegram&Date)

## Acknowledgments

- [Claude](https://claude.ai) by Anthropic
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
