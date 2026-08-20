"""Slash-command registry and dispatcher."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable


class CommandHandler(Protocol):
    """Signature every command handler must match.

    Parameters
    ----------
    args : str
        The raw argument string *after* the command name (may be empty).
    state : dict
        Mutable session state shared with the REPL loop
        (messages, config, console, …).

    Returns
    -------
    exit_bool : bool | None
        ``True`` → the REPL should terminate.  ``False`` / ``None`` → stay alive.
    """

    def __call__(self, args: str, state: dict) -> bool | None: ...


# ---------------------------------------------------------------------------
# Registry — just register new entries here (or call register() at runtime).
# ---------------------------------------------------------------------------

_commands: dict[str, tuple[CommandHandler, str]] = {}


def register(name: str, description: str):
    """Decorator to register a command.

    Usage::

        @register("/exit", "Exit the session.")
        def handler(args: str, state: dict) -> bool | None: ...
    """

    def wrapper(func: CommandHandler) -> CommandHandler:
        _commands[name] = (func, description)
        return func

    return wrapper


def list_commands() -> Iterable[tuple[str, str]]:
    """Return ``[(name, description), ...]`` sorted alphabetically."""
    return sorted((name, desc) for name, (_, desc) in _commands.items())


def is_registered(name: str) -> bool:
    """Check if a command name exists in the registry."""
    return name in _commands


def run(name: str, args: str, state: dict) -> bool | None:
    """Look up *name* and invoke its handler.  Returns ``None`` for unknown commands."""
    entry = _commands.get(name)
    if entry is None:
        return None  # not a registered command → treat as normal message
    handler, _ = entry
    return handler(args, state)


# ---------------------------------------------------------------------------
# Built-in commands
# ---------------------------------------------------------------------------

@register("/exit", "Exit the session.")
def cmd_exit(args: str, state: dict) -> bool | None:
    """Hard exit — tells the REPL to terminate."""
    return True


@register("/export", "Export all messages to a timestamped text file.")
def cmd_export(args: str, state: dict) -> bool | None:
    """Export the full conversation history to a local .txt file.

    The file is written inside the current working directory with a name
    like ``2026-08-20-152519-session.txt`` so successive exports never
    overwrite each other.

    If the user passes ``--exit`` or ``-e`` as an argument, returns
    ``True`` to terminate the REPL. Otherwise keeps the session alive.
    """
    messages = state.get("messages", [])
    if not messages:
        # Nothing to export — signal via output if possible.
        append = state.get("_append")
        if append is not None:
            append("\n⚠  No messages to export.\n\n")
        return None

    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d-%H%M%S")
    filename = f"{timestamp}-session.txt"
    filepath = Path.cwd() / filename

    # Parse optional arguments.
    raw_args = args.strip().lower()
    should_exit = raw_args in {"--exit", "-e"}

    lines: list[str] = []
    separator = "─" * 60

    for msg in messages:
        role = msg.get("role", "unknown").capitalize()
        content = msg.get("content", "")
        lines.append(separator)
        lines.append(f"[{role}]")
        lines.append(separator)
        lines.append(content)
        lines.append("")

    try:
        filepath.write_text("\n".join(lines), encoding="utf-8")
        append = state.get("_append")
        if append is not None:
            append(f"\n✓  Exported {len(messages)} message(s) to {filepath.name}\n\n")
        return True if should_exit else None
    except OSError as exc:
        append = state.get("_append")
        if append is not None:
            append(f"\n⚠  Failed to write {filepath}: {exc}\n\n")
        return None


@register("/clear", "Clear conversation history and start a fresh session.")
def cmd_clear(args: str, state: dict) -> bool | None:
    """Clear the scrollback pane and in-memory message list."""
    reset_fn = state.get("_reset")
    if reset_fn is not None:
        reset_fn()
    append = state.get("_append")
    if append is not None:
        append("\n✓  Session cleared.\n\n")
    return None


@register("/context", "Show current context token usage and model info.")
def cmd_context(args: str, state: dict) -> bool | None:
    """Display a summary of token usage for the active session.

    Reads cumulative token counts from ``_tokens`` (populated by the
    streaming worker via LiteLLM usage blocks).  Falls back gracefully
    when usage data is unavailable.

    Output mirrors Claude Code's /context style: model + effort on top,
    a block-based progress bar, and a per-category breakdown with
    token counts and percentages.
    """
    tokens = state.get("_tokens") or {}
    append = state.get("_append")
    if append is None:
        return None

    # -----------------------------------------------------------------------
    # Gather model info
    # -----------------------------------------------------------------------
    config = state.get("_config")
    model_name = getattr(config, "model", "") or ""
    effort = getattr(config, "effort", "") or ""
    ctx_limit = get_context_window(model_name)

    # -----------------------------------------------------------------------
    # Compute category tokens
    # -----------------------------------------------------------------------
    prompt_tokens = tokens.get("prompt", 0)
    completion_tokens = tokens.get("completion", 0)
    thinking_tokens = tokens.get("thinking", 0)
    total_used = prompt_tokens + completion_tokens + thinking_tokens

    # System prompt tokens: estimate from the number of messages (each turn
    # re-sends the full message list, so we count roughly one system-ish
    # header per turn).  For simplicity we use half the prompt budget minus
    # what's clearly user/assistant content.
    messages = state.get("messages", [])
    msg_tokens = 0
    try:
        import litellm
        msg_tokens = litellm.token_counter(messages=messages) or 0
    except Exception:
        msg_tokens = sum(len(m.get("content", "")) for m in messages) // 4

    # Prompt overhead (the remainder of prompt tokens after message content)
    # is the system / API header that gets sent each turn.
    system_tokens = max(prompt_tokens - msg_tokens, 0)
    total_displayed = system_tokens + msg_tokens + thinking_tokens + completion_tokens

    free = max((ctx_limit or 0) - total_displayed, 0)
    pct_used = (total_displayed / ctx_limit * 100) if ctx_limit and total_displayed > 0 else 0.0

    # -----------------------------------------------------------------------
    # Build block bar (32 blocks → each ~3.125% of the context window)
    # -----------------------------------------------------------------------
    bar_width = 32
    filled = round(pct_used / 100 * bar_width) if ctx_limit else 0
    empty = bar_width - filled
    full_block = "█"
    empty_block = "░"
    bar = full_block * filled + empty_block * empty

    # -----------------------------------------------------------------------
    # Render
    # -----------------------------------------------------------------------
    lines: list[str] = []
    sep = "─" * 52

    lines.append(f"\n{sep}")
    lines.append(" Context Usage")

    if model_name:
        effort_str = f" · effort: {effort}" if effort else ""
        lines.append(f"  {model_name}{effort_str}")

    if ctx_limit and total_displayed > 0:
        lines.append(
            f"  {bar}  "
            f"{format_tokens_h(total_displayed)}/{format_tokens_h(ctx_limit)} tokens ({pct_used:.0f}%)"
        )
    elif ctx_limit:
        lines.append(f"  {empty_block * bar_width}  0/{format_tokens_h(ctx_limit)} tokens (0%)")
    else:
        lines.append(f"  Used: {format_tokens_h(total_displayed)} tokens (context window unknown)")

    lines.append("")
    lines.append(" Estimated usage by category")

    if system_tokens > 0:
        pct_sys = system_tokens / total_displayed * 100 if total_displayed else 0
        lines.append(f"   System prompt: {format_tokens_h(system_tokens)} tokens ({pct_sys:.1f}%)")

    if msg_tokens > 0:
        pct_msg = msg_tokens / total_displayed * 100 if total_displayed else 0
        lines.append(f"   Messages:      {format_tokens_h(msg_tokens)} tokens ({pct_msg:.1f}%)")

    if thinking_tokens > 0:
        pct_think = thinking_tokens / total_displayed * 100 if total_displayed else 0
        lines.append(f"   Thinking:      {format_tokens_h(thinking_tokens)} tokens ({pct_think:.1f}%)")

    if completion_tokens > 0:
        pct_comp = completion_tokens / total_displayed * 100 if total_displayed else 0
        lines.append(f"   Completion:    {format_tokens_h(completion_tokens)} tokens ({pct_comp:.1f}%)")

    if ctx_limit and free > 0:
        pct_free = free / ctx_limit * 100
        lines.append(f"   Free space:    {format_tokens_h(free)} ({pct_free:.1f}%)")

    lines.append(sep)
    lines.append("")

    append("\n".join(lines))
    return None


# ---------------------------------------------------------------------------
# Helpers for /context
# ---------------------------------------------------------------------------

def format_tokens_h(n: int) -> str:
    """Format an integer token count with a human-friendly suffix.

    Examples: ``13`` → ``13``, ``6_620`` → ``6.6k``, ``200_000`` → ``200.0k``.
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_tokens(n: int) -> str:
    """Alias for backward compat."""
    return format_tokens_h(n)


def get_context_window(model: str) -> int | None:
    """Return the approximate context window (in tokens) for *model*.

    Only the most common models are listed; unknown models return ``None``.
    """
    lookup: dict[str, int] = {
        # Claude
        "claude-sonnet-4-6": 200_000,
        "claude-sonnet-4-6-20250514": 200_000,
        "claude-sonnet-4": 200_000,
        "claude-opus-5": 200_000,
        "claude-opus-4": 200_000,
        "claude-haiku-4-5-20251001": 200_000,
        "claude-haiku-3-5": 200_000,
        "claude-3-5-sonnet-20241022": 200_000,
        "claude-3-opus-20240229": 200_000,
        "claude-3-haiku-20240307": 200_000,
        # GPT
        "gpt-4o-mini": 128_000,
        "gpt-4o": 128_000,
        "gpt-4-turbo": 128_000,
        "gpt-4": 8_192,
        "gpt-3.5-turbo": 16_385,
        # Gemini
        "gemini-2.5-pro": 1_048_576,
        "gemini-2.0-pro-exp-02-05": 2_097_152,
        "gemini-2.0-flash": 1_048_576,
        "gemini-1.5-pro": 2_097_152,
        "gemini-1.5-flash": 1_048_576,
        # Qwen
        "qwen-qaqwen3-coder-plus": 131_072,
        "qwen/qwen3-coder-plus": 131_072,
        "qwen3-coder-plus": 131_072,
        "qwen/qwen3-235b-a22b": 131_072,
        "qwen3-235b-a22b": 131_072,
        "qwen/qwen3-235b-a22b-thinking-2507": 131_072,
        "qwen/qwen3-14b": 131_072,
        "qwen3-14b": 131_072,
        "qwen/qwen3-32b": 131_072,
        "qwen3-32b": 131_072,
        "qwen/qwen3-30b-a3b": 131_072,
        "qwen3-30b-a3b": 131_072,
        "qwen/qwen3-32b-preview": 131_072,
        "qwen3-32b-preview": 131_072,
        "qwen/qwen2.5-coder-32b": 131_072,
        "qwen2.5-coder-32b": 131_072,
        "qwen/qwen2.5-72b-instruct": 131_072,
        "qwen2.5-72b-instruct": 131_072,
        "qwen/qwen2.5-7b-instruct": 131_072,
        "qwen2.5-7b-instruct": 131_072,
        "qwen/qwen2-72b-instruct": 32_768,
        "qwen2-72b-instruct": 32_768,
        "qwen/qwen-turbo": 1_000_000,
        "qwen-turbo": 1_000_000,
        "qwen/qwen-plus": 131_072,
        "qwen-plus": 131_072,
        # Broad Qwen prefixes (checked last due to shorter length) — catch all variants
        "qwen/qwen3": 131_072,
        "qwen3": 131_072,
        "qwen/qwen2.5": 131_072,
        "qwen2.5": 131_072,
        # DeepSeek
        "deepseek-chat": 128_000,
        "deepseek-reasoner": 64_000,
        "deepseek-coder-v2": 128_000,
        "deepseek-v3": 128_000,
        "deepseek-r1": 128_000,
        # Misc / Open-weight
        "llama-3.3-70b": 128_000,
        "llama-3.1-405b": 131_072,
        "llama-3.1-70b": 131_072,
        "llama-3-70b": 8_192,
        "mistral-large": 128_000,
        "mistral-small": 32_768,
        "command-a-plus": 256_000,
        "command-r-plus": 128_000,
        "command-r": 128_000,
    }

    if not model:
        return None

    # Exact match first.
    if model in lookup:
        return lookup[model]

    # Prefix match for versioned aliases (e.g. "claude-sonnet-4-6-v2").
    for key in sorted(lookup, key=len, reverse=True):
        if model.startswith(key):
            return lookup[key]

    return None
