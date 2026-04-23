"""Bash directory boundary enforcement for Claude tool calls."""

import shlex
from pathlib import Path
from typing import Optional, Set, Tuple

# Subdirectories under ~/.claude/ that Claude Code uses internally.
_CLAUDE_INTERNAL_SUBDIRS: Set[str] = {"plans", "todos", "settings.json"}

# Commands that modify the filesystem or change context and should have paths checked
_FS_MODIFYING_COMMANDS: Set[str] = {
    "mkdir",
    "touch",
    "cp",
    "mv",
    "rm",
    "rmdir",
    "ln",
    "install",
    "tee",
    "cd",
}

# Read-only commands that take NO filesystem paths at all — safe to
# skip path validation entirely. ``echo`` / ``printf`` print literals;
# ``pwd`` / ``whoami`` / ``date`` / ``env`` / ``printenv`` return
# process state; ``which`` resolves command names; ``dirname`` /
# ``basename`` are pure string ops on path-like arguments, not file
# accesses.
_NO_PATH_COMMANDS: Set[str] = {
    "whoami",
    "pwd",
    "echo",
    "printf",
    "env",
    "printenv",
    "date",
    "which",
    "dirname",
    "basename",
}

# Read-only commands that DO take filesystem paths and must have those
# paths validated. Pre-H3 these lived in a single
# ``_READ_ONLY_COMMANDS`` bucket that skipped path validation entirely,
# which meant ``cat /etc/shadow`` / ``tail ~/.ssh/id_rsa`` /
# ``stat /home/other-user/`` passed the boundary check and reached the
# OS — a data-exfil path any authenticated user could pull via Claude.
# Reads outside ``APPROVED_DIRECTORY`` are disclosure, even without
# mutation.
_READ_WITH_PATHS_COMMANDS: Set[str] = {
    "cat",
    "ls",
    "head",
    "tail",
    "less",
    "more",
    "wc",
    "sort",
    "uniq",
    "diff",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "realpath",
}

# Actions / expressions that make ``find`` a filesystem-modifying command
_FIND_MUTATING_ACTIONS: Set[str] = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}

# Bash command separators
_COMMAND_SEPARATORS: Set[str] = {"&&", "||", ";", "|", "&"}


def check_bash_directory_boundary(
    command: str,
    working_directory: Path,
    approved_directory: Path,
) -> Tuple[bool, Optional[str]]:
    """Check if a bash command's paths stay within the approved directory."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        # If we can't parse the command, let it through —
        # the sandbox will catch it at the OS level
        return True, None

    if not tokens:
        return True, None

    # Split tokens into individual commands based on separators
    command_chains: list[list[str]] = []
    current_chain: list[str] = []

    for token in tokens:
        if token in _COMMAND_SEPARATORS:
            if current_chain:
                command_chains.append(current_chain)
            current_chain = []
        else:
            current_chain.append(token)

    if current_chain:
        command_chains.append(current_chain)

    resolved_approved = approved_directory.resolve()

    # Check each command in the chain
    for cmd_tokens in command_chains:
        if not cmd_tokens:
            continue

        base_command = Path(cmd_tokens[0]).name

        # No-path commands (echo, pwd, whoami, …) are always allowed.
        if base_command in _NO_PATH_COMMANDS:
            continue

        # Determine if this specific command in the chain needs path validation
        needs_check = False
        if base_command == "find":
            needs_check = any(t in _FIND_MUTATING_ACTIONS for t in cmd_tokens[1:])
        elif base_command in _FS_MODIFYING_COMMANDS:
            needs_check = True
        elif base_command in _READ_WITH_PATHS_COMMANDS:
            # H3 — path-bearing read commands must also stay inside the
            # approved directory. Read access to ``/etc/shadow`` is
            # data disclosure even without mutation.
            needs_check = True

        if not needs_check:
            continue

        # Check each argument for paths outside the boundary
        for token in cmd_tokens[1:]:
            # Skip flags
            if token.startswith("-"):
                continue

            # Resolve both absolute and relative paths against the working
            # directory so that traversal sequences like ``../../evil`` are
            # caught instead of being silently allowed.
            try:
                if token.startswith("/"):
                    resolved = Path(token).resolve()
                else:
                    resolved = (working_directory / token).resolve()

                if not _is_within_directory(resolved, resolved_approved):
                    return False, (
                        f"Directory boundary violation: '{base_command}' targets "
                        f"'{token}' which is outside approved directory "
                        f"'{resolved_approved}'"
                    )
            except (ValueError, OSError):
                # If path resolution fails, the command might be malformed or
                # using bash features we can't statically analyze.
                # We skip checking this token and rely on the OS-level sandbox.
                continue

    return True, None


def _is_claude_internal_path(file_path: str) -> bool:
    """Check whether *file_path* points inside ``~/.claude/`` (allowed subdirs only)."""
    try:
        resolved = Path(file_path).resolve()
        home = Path.home().resolve()
        claude_dir = home / ".claude"

        # Path must be inside ~/.claude/
        try:
            rel = resolved.relative_to(claude_dir)
        except ValueError:
            return False

        # Must be in one of the known subdirectories (or a known file)
        top_part = rel.parts[0] if rel.parts else ""
        return top_part in _CLAUDE_INTERNAL_SUBDIRS

    except Exception:
        return False


def _is_within_directory(path: Path, directory: Path) -> bool:
    """Check if path is within directory."""
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False
