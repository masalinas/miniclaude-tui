"""Main REPL — full-screen Application with a pinned bottom prompt.

Layout (top to bottom, via HSplit):
    header_bar      -- 1-row full-width status bar (model / effort / cwd)
    header_field    -- scrollable, read-only output/history pane (fills space)
    white_separator -- a white separator line
    prompt_field    -- the "> " input row, grows with actual typed content
    white_separator -- a white separator line
    bottom_window   -- 1-row status/hint bar, always the last row

Wrapped in a FloatContainer so the slash-command CompletionsMenu can float
over the prompt row without disturbing the HSplit sizing.
"""

from __future__ import annotations

import os
import shutil
import time
import re
import contextlib
import threading
import tomllib
from pathlib import Path
from itertools import cycle
import asyncio
from asyncio import subprocess

import pyperclip

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.filters import has_focus
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Float, FloatContainer, HSplit, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea, VerticalLine
from prompt_toolkit.document import Document
from prompt_toolkit.lexers import Lexer

from agent.config import Config
from agent.commands import is_registered, list_commands, run as run_command
from agent.ui import format_error, StreamRenderer, format_status_line

SPINNER_FRAMES = ["✻", "✽", "✾", "✿", "❁"]
spinner = cycle(SPINNER_FRAMES)

MAIN_COLOR = "#62A35B"
LOOP_COLOR = f"fg:{MAIN_COLOR}"
TIMING_COLOR = f"fg:{MAIN_COLOR}"

TIMING_PATTERN = re.compile(r"Thought for|Crunched for")

EDGE_MARGIN = 2          # rows from top/bottom that trigger auto-scroll
EDGE_SCROLL_INTERVAL = 0.08
EDGE_SCROLL_STEP = 2

_drag_active = False
_edge_scroll_task: asyncio.Task | None = None

_copy_status_text = ""
_copy_status_task: asyncio.Task | None = None

def get_app_version() -> str:
    """Reads the project version from pyproject.toml."""
    pyproject_path = Path(__name__).parent / "pyproject.toml"
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
            # Fetch from [project] section (standard PEP 621)
            return data.get("project", {}).get("version", "0.1.0")
    except Exception:
        return "0.1.0"

# Dynamic Frame creation
version = get_app_version()

# ---------------------------------------------------------------------------
# Tab-completer for slash commands
# ---------------------------------------------------------------------------
class SlashCompleter(Completer):
    """Complete registered commands when the user types ``/``."""

    def get_completions(self, document, complete_event: CompleteEvent):
        text = document.text_before_cursor.rstrip()
        parts = text.rsplit(maxsplit=1)
        word = parts[-1] if parts and parts[-1].startswith("/") else ""
        if not word.startswith("/"):
            return
        for name, desc in list_commands():
            if name.startswith(word):
                yield Completion(
                    text=name,
                    start_position=-len(word),
                    display_meta=f"{name} — {desc}",
                )

# ---------------------------------------------------------------------------
# Shared state (config, conversation, ctrl-c hint)
# ---------------------------------------------------------------------------
cfg = Config.from_env()
messages: list[dict[str, str]] = []
cmd_state: dict

_ctrl_c = {"timer": None}

# Track the current LLM turn task so Ctrl+C can cancel it.
_current_turn_task: asyncio.Task | None = None

# Event set when the user wants to cancel the in-flight LLM request.
_cancel_event: threading.Event | None = None

# Per-turn usage returned by the stream worker.  Written by the background
# thread, read after ``asyncio.to_thread`` returns.
_turn_usage: dict[str, int] = {}

# Cumulative session token counts (thread-safe via turn-serialization).
_session_tokens: dict[str, int] = {
    "prompt": 0,
    "completion": 0,
    "thinking": 0,
}

# ---------------------------------------------------------------------------
# Top status bar — a real full-width Window (like the bottom bar), not text
# baked into header_field. Fixes it looking like a truncated/cut-off rule
# on wide terminals.
# ---------------------------------------------------------------------------
def _get_short_cwd(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path

def create_header():
    cwd = _get_short_cwd(os.getcwd())
    
    # Left Column
    left_content = FormattedTextControl([
        ("class:banner-welcome", "Welcome back!\n\n"),
        ("class:accent-icon", "   ▄▀▄▀▄   \n"),
        ("class:accent-icon", "  █▀   ▀█  \n\n"),
        ("class:banner-sub", f"{cfg.model} with {cfg.effort} effort\n"),
        ("class:banner-sub", f"cwd: {cwd}\n"),
    ])

    left_window = Window(
        content=left_content,
        align=WindowAlign.CENTER,
        dont_extend_width=True,
    )

    # Right Column: Upper section (Tips)
    tips_window = Window(
        content=FormattedTextControl([
            ("class:section-title", "Tips for getting started\n"),
            ("class:body-text", "Run "),
            ("class:code-text", "/help"),
            ("class:body-text", " to list available commands"),
        ]),
        dont_extend_height=True,
    )

    # Right Column: Horizontal rule
    rule_window = Window(
        char="─",
        style="class:frame.border",
        height=1
    )

    # Right Column: Lower section (What's new)
    news_window = Window(
        content=FormattedTextControl([
            ("class:section-title", "What's new\n"),
            ("class:body-text", "Fixed messages selected\n"),
            ("class:body-text", "Add clipboard copy/paste integration\n"),
            ("class:sub-link", "/help for more"),
        ]),
        dont_extend_height=True,
    )

    # Right Column Assembly
    right_side = HSplit([
        tips_window,
        rule_window,
        news_window,
    ])

    # Main Layout Assembly with VerticalLine
    body = VSplit([
        Window(width=2),
        left_window,
        Window(width=2),
        VerticalLine(),  # <--- Automatically matches exact vertical height
        Window(width=2),
        right_side,
    ])

    return Frame(
        body=body,
        # Passing FormattedText / HTML left-aligns the title and allows inline colors
        title=HTML(f'<b>MiniClaude TUI</b> <style fg="#a0a0a0">v{get_app_version()}</style>'),
    )

header_bar = create_header()

# ---------------------------------------------------------------------------
# Copy to clipboard helpers
# ---------------------------------------------------------------------------
class LoopColorAppLexer(Lexer):
    def lex_document(self, document):
        def get_line(lineno):
            line = document.lines[lineno]
            if "Loop running" in line:
                return [(LOOP_COLOR, line)]
            if TIMING_PATTERN.search(line):
                return [(TIMING_COLOR, line)]
            return [("", line)]
        return get_line

def _copy_to_system_clipboard(text: str) -> bool:
    """Try a real clipboard tool first (subprocess return code tells us
    definitively whether it worked). Fall back to OSC 52 only if no
    tool is installed — and even then, treat it as unverified, since
    the terminal never confirms whether it honored the sequence."""
    candidates = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["pbcopy"],
    ]

    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except Exception:
            continue

    pyperclip.copy(text)

    return False  # report "unverified" rather than claiming success

def _set_copy_status(text: str) -> None:
    global _copy_status_text, _copy_status_task

    ok = _copy_to_system_clipboard(text)
    if ok:
        _copy_status_text = f"Copied {len(text)} characters to clipboard"
    else:
        _copy_status_text = (
            f"Selected {len(text)} chars."
        )
    app.invalidate()

def _stop_edge_scroll() -> None:
    global _edge_scroll_task

    if _edge_scroll_task is not None:
        _edge_scroll_task.cancel()
        _edge_scroll_task = None

def _start_edge_scroll(direction: int) -> None:
    """direction: -1 to scroll up, +1 to scroll down. No-op if already running."""
    global _edge_scroll_task

    if _edge_scroll_task is not None:
        return

    async def _run() -> None:
        try:
            while True:
                w = header_field.window
                if direction < 0:
                    w.vertical_scroll = max(0, w.vertical_scroll - EDGE_SCROLL_STEP)
                else:
                    w.vertical_scroll += EDGE_SCROLL_STEP
                app.invalidate()
                await asyncio.sleep(EDGE_SCROLL_INTERVAL)
        except asyncio.CancelledError:
            pass

    _edge_scroll_task = asyncio.create_task(_run())

def _wrap_copy_on_release(control):
    original_handler = control.mouse_handler

    def handler(mouse_event):
        global _drag_active

        result = original_handler(mouse_event)  # let selection extend normally first
        et = mouse_event.event_type

        if et == MouseEventType.MOUSE_DOWN:
            _drag_active = True

            _stop_edge_scroll()
        elif et == MouseEventType.MOUSE_MOVE and _drag_active:
            info = header_field.window.render_info
            if info is not None:
                y = mouse_event.position.y
                height = info.window_height

                if y <= EDGE_MARGIN:
                    _start_edge_scroll(-1)
                elif y >= height - EDGE_MARGIN:
                    _start_edge_scroll(1)
                else:
                    _stop_edge_scroll()
        elif et == MouseEventType.MOUSE_UP:
            _drag_active = False
            _stop_edge_scroll()

            buf = control.buffer
            if buf.selection_state is not None:
                data = buf.copy_selection()
                text = data.text
                if text:
                    _set_copy_status(text)

        return result

    control.mouse_handler = handler

header_field = TextArea(
    text="Type your prompt below. /help for commands.\n\n",
    read_only=True,
    scrollbar=True,
    wrap_lines=True,
    focus_on_click=True,
    lexer=LoopColorAppLexer(),
    # dont_extend_height left at its default (False): this is the ONE pane
    # that should claim leftover vertical space and fill the screen.
)

_wrap_copy_on_release(header_field.control)

# ---------------------------------------------------------------------------
# Output helpers (thread-safe: schedule onto the app's own event loop)
# ---------------------------------------------------------------------------
def _append_output(text: str) -> None:
    """Append text to the scrollback pane and scroll it into view."""
    buf = header_field.buffer
    new_text = buf.text + text
    sel = buf.selection_state

    # set_document lets you update text and cursor position simultaneously
    buf.set_document(
        Document(new_text, cursor_position=len(new_text)),
        bypass_readonly=True
    )
    buf.selection_state = sel

    app.invalidate()

def _append_output_threadsafe(text: str, loop: asyncio.AbstractEventLoop) -> None:
    loop.call_soon_threadsafe(_append_output, text)

def _reset_pane() -> None:
    """Clear the scrollback pane and reset conversation history."""
    buf = header_field.buffer
    buf.set_document(
        Document("Type your prompt below. /help for commands.\n\n", cursor_position=0),
        bypass_readonly=True,
    )
    messages.clear()
    _session_tokens["prompt"] = 0
    _session_tokens["completion"] = 0
    _session_tokens["thinking"] = 0
    app.invalidate()

# Populate the mutable session state dict shared with command handlers.
cmd_state = {
    "messages": messages,
    "_config": cfg,
    "_append": _append_output,
    "_reset": _reset_pane,
    "_tokens": _session_tokens,
}

# ---------------------------------------------------------------------------
# Streaming via LiteLLM SDK (blocking generator run off the UI thread)
# ---------------------------------------------------------------------------
def _run_stream_worker(
    msgs: list[dict[str, str]],
    renderer: StreamRenderer,
    cancel_event: threading.Event,
) -> None:
    """Runs in a background thread — iterates the blocking litellm stream.

    All UI writes go through ``renderer``'s callback, which marshals them
    back onto the prompt_toolkit event loop via call_soon_threadsafe, so
    this function never touches widgets directly.

    If ``cancel_event`` is set (user pressed Ctrl+C), the underlying HTTP
    request is closed and iteration stops immediately.
    """
    import litellm

    litellm.set_verbose = False
    litellm.suppress_debug_info = True

    if cfg.api_base and "openrouter" in cfg.api_base:
        model_name = f"openrouter/{cfg.model}"
    elif cfg.api_base:
        model_name = f"openai/{cfg.model}"
    else:
        model_name = cfg.model

    response = litellm.completion(
        model=model_name, messages=msgs, **cfg.completion_kwargs()
    )

    try:
        thinking_started = False
        for chunk in response:
            if cancel_event.is_set():
                break

            # Capture usage from the last chunk (most providers attach it to
            # the final streaming block).
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                _turn_usage["prompt"] = getattr(usage, "prompt_tokens", 0) or 0
                _turn_usage["completion"] = (
                    getattr(usage, "completion_tokens", 0) or 0
                )
                # Some providers report thinking/reasoning tokens separately.
                _turn_usage["thinking"] = (
                    getattr(usage, "prompt_cache_read_input_tokens", 0) or 0
                )

            if not chunk or not getattr(chunk, "choices", None):
                continue

            delta = chunk.choices[0].delta

            raw_reasoning = getattr(delta, "reasoning_content", None)
            if raw_reasoning is not None:
                renderer.begin_thinking()
                thinking_started = True
                continue

            text = getattr(delta, "content", None) or ""
            if text:
                if thinking_started:
                    renderer.begin_content()
                    thinking_started = False
                renderer.append(text)
    finally:
        # Close the underlying HTTP connection so the request is cancelled.
        if hasattr(response, "__exit__"):
            response.__exit__(None, None, None)
        elif hasattr(response, "close"):
            response.close()
        elif hasattr(response, "_http_session") and response._http_session:
            response._http_session.close()

def _estimate_tokens(msgs: list[dict[str, str]]) -> dict[str, int]:
    """Fallback token estimator when the streaming API returns no usage.

    Uses LiteLLM's built-in ``token_counter`` (tiktoken-based) to count
    total tokens, then splits into prompt vs completion by role.
    Returns ``{"prompt": ..., "completion": ..., "thinking": ...}``.
    """
    try:
        import litellm

        total = litellm.token_counter(messages=msgs) or 0

        # Count user message tokens as "prompt", assistant as "completion".
        user_msgs = [{"role": m["role"], "content": m["content"]}
                     for m in msgs if m.get("role") == "user"]
        assistant_msgs = [{"role": m["role"], "content": m["content"]}
                          for m in msgs if m.get("role") == "assistant"]

        prompt_tokens = litellm.token_counter(messages=user_msgs) or 0
        completion_tokens = (litellm.token_counter(messages=assistant_msgs) or 0)
    except Exception:
        total = sum(len(m.get("content", "")) for m in msgs) // 4
        prompt_tokens = sum(
            len(m.get("content", "")) for m in msgs if m.get("role") == "user"
        ) // 4
        completion_tokens = sum(
            len(m.get("content", "")) for m in msgs if m.get("role") == "assistant"
        ) // 4

    return {
        "prompt": prompt_tokens,
        "completion": completion_tokens,
        "thinking": 0,
    }

async def _handle_turn(text: str) -> None:
    global _current_turn_task, _cancel_event

    messages.append({"role": "user", "content": text})
    turn_start = time.monotonic()
    loop = asyncio.get_running_loop()

    current_label = f"{SPINNER_FRAMES[0]} Loop running ...\n\n"
    _append_output(current_label)

    # Background task to cycle spinner frames while waiting for worker
    async def _animate_spinner() -> None:
        nonlocal current_label
        frame_idx = 0
        while True:
            await asyncio.sleep(0.12)
            frame_idx = (frame_idx + 1) % len(SPINNER_FRAMES)
            next_label = f"{SPINNER_FRAMES[frame_idx]} Loop running ...\n\n"

            buf = header_field.buffer
            if current_label in buf.text:
                new_text = buf.text.replace(current_label, next_label, 1)
                buf.set_document(
                    Document(new_text, cursor_position=len(new_text)),
                    bypass_readonly=True,
                )
                app.invalidate()
                current_label = next_label

    spinner_task = asyncio.create_task(_animate_spinner())

    renderer = StreamRenderer(
        on_update=lambda delta: _append_output_threadsafe(delta, loop)
    )

    # Cancellation signal shared with the background thread.
    _cancel_event = threading.Event()

    try:
        await asyncio.to_thread(_run_stream_worker, messages, renderer, _cancel_event)
    except Exception as exc:
        msg = str(exc)
        if len(msg) > 300:
            msg = msg[:297] + "..."
        _append_output(format_error(f"API error — {msg}"))
        return
    finally:
        # Stop spinner animation
        spinner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await spinner_task

        # Remove whichever icon frame was active when finished
        buf = header_field.buffer
        new_text = buf.text.replace(current_label, "", 1)
        buf.set_document(
            Document(new_text, cursor_position=len(new_text)),
            bypass_readonly=True,
        )
        app.invalidate()

        # Clear tracking references now the turn has completed.
        if _current_turn_task is asyncio.current_task():
            _current_turn_task = None
        _cancel_event = None

    _append_output(renderer.footer(turn_start))

    if renderer.text:
        messages.append({"role": "assistant", "content": renderer.text})

    # Accumulate turn-level token counts into session totals.
    # Fallback to estimation when the streaming API didn't report usage.
    if _turn_usage:
        for key in ("prompt", "completion", "thinking"):
            _session_tokens[key] += _turn_usage.get(key, 0)
    else:
        estimated = _estimate_tokens(messages)
        for key in ("prompt", "completion", "thinking"):
            _session_tokens[key] += estimated.get(key, 0)

    # Reset per-turn tracking so it's fresh for the next turn.
    _turn_usage.clear()

# ---------------------------------------------------------------------------
# Command dispatch / input submission
# ---------------------------------------------------------------------------
def _print_help() -> str:
    lines = ["\nAvailable commands:"]
    for name, desc in list_commands():
        lines.append(f"  {name}   {desc}")
    lines += [
        "",
        "Keyboard shortcuts:",
        "  Ctrl+C      Cancel / press twice quickly to exit.",
        "  Ctrl+D      Exit the session.",
        "  \\ + Enter   Insert a newline in multiline input.",
        "\n",
    ]
    return "\n".join(lines)

def _on_submit(buff) -> None:
    """Buffer accept_handler — MUST be passed into TextArea's constructor
    (see prompt_field below), not assigned as an attribute afterward, or
    the underlying Buffer never learns about it and Enter does nothing.
    """
    text = buff.text.strip()
    if not text:
        return

    _append_output(f"\n❯  {text}\n\n")

    if text == "/help":
        _append_output(_print_help())
        return

    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd_name = parts[0]
        cmd_args = parts[1] if len(parts) > 1 else ""

        if not is_registered(cmd_name):
            _append_output(f"Unknown command: {cmd_name}. Type /help for available commands.\n")
            return

        # NOTE: run_command / individual command handlers must not call
        # rich's console.print() directly in full-screen mode — route any
        # output they produce through _append_output instead.
        result = run_command(cmd_name, cmd_args, cmd_state)
        if result is True:
            app.exit()
        return

    _current_turn_task = asyncio.create_task(_handle_turn(text))

history_path = Path.home() / ".cache" / "agent_cli" / "history.txt"
history_path.parent.mkdir(parents=True, exist_ok=True)

prompt_field = TextArea(
    prompt=HTML('<b>❯</b> '),
    focus_on_click=True,
    multiline=True,
    wrap_lines=True,
    completer=SlashCompleter(),
    complete_while_typing=True,
    history=FileHistory(str(history_path)),
    accept_handler=_on_submit,   # <-- wired here, at construction time
    dont_extend_height=True,     # <-- grows with real content only; never
                                 #     claims leftover screen space, so it
                                 #     can't balloon to a fixed max full of
                                 #     blank lines.
)

# White separator lines around the prompt
white_separator = Window(
    char="─",
    style="fg:#ffffff",
    height=1
)

def _bottom_bar_text():
    # 1. Retrieve current terminal width dynamically
    try:
        width = get_app().output.get_size().columns
    except Exception:
        width = 80

    # 2. Handle Ctrl+C press state
    if _ctrl_c["timer"] is not None:
        text = "Press Ctrl-C again to exit."
        spaces_needed = max(0, width - len(text))
        full_text = text + (" " * spaces_needed)
        return HTML(f'<style bg="#262626" fg="#d78700">{full_text}</style>')

    if _copy_status_text:
        spaces_needed = max(0, width - len(_copy_status_text))
        full_text = _copy_status_text + (" " * spaces_needed)
        return HTML(f'<style bg="#262626" fg="#5fd75f">{full_text}</style>')

    # 3. Handle default status bar state
    base_text = "[Enter] Submit  ·  [\\ + Enter] Newline  ·  [Ctrl+C] Exit  ·  "
    spaces_needed = max(0, width - len(base_text))
    full_text = base_text + (" " * spaces_needed)

    return HTML(f'<style bg="#262626" fg="#8a8a8a">{full_text}</style>')

bottom_window = Window(
    height=1,
    content=FormattedTextControl(_bottom_bar_text),
    style="class:status-bar",
)

# ---------------------------------------------------------------------------
# Key bindings
# ---------------------------------------------------------------------------
kb = KeyBindings()

@kb.add("enter", filter=has_focus(prompt_field), eager=True)
def _enter(event):
    """Submit unless the line ends with ``\\`` (insert a newline instead)."""
    buf = event.current_buffer
    text = buf.document.text_before_cursor
    if text.endswith("\\"):
        buf.text = text[:-1] + "\n"
        buf.cursor_position = len(text[:-1]) + 1
        return
    buf.validate_and_handle()


@kb.add("escape", eager=True)
def _dismiss_completions(event):
    """Close the completion popup menu and clear partial command input."""
    buf = event.current_buffer
    if buf.complete_state is not None:
        buf.cancel_completion()
        # Also clear the typed text that triggered the completion.
        buf.reset()
        event.app.invalidate()


@kb.add("c-c")
def _cancel(event):
    """First Ctrl+C cancels in-flight LLM requests + clears the buffer.
    Second press within 1s exits."""
    global _current_turn_task

    # Cancel any in-flight LLM request.
    if _cancel_event is not None:
        _cancel_event.set()

    if _current_turn_task is not None and not _current_turn_task.done():
        _current_turn_task.cancel()
        _current_turn_task = None

    now = time.monotonic()
    if _ctrl_c["timer"] is None or (now - _ctrl_c["timer"]) > 1.0:
        _ctrl_c["timer"] = now
        prompt_field.buffer.reset()
        event.app.invalidate()

        async def _clear_hint():
            await asyncio.sleep(1.1)
            _ctrl_c["timer"] = None
            event.app.invalidate()

        asyncio.create_task(_clear_hint())
    else:
        event.app.exit()


@kb.add("c-d")
def _exit(event):
    event.app.exit()

# ---------------------------------------------------------------------------
# Layout / Application
# ---------------------------------------------------------------------------
root_container = FloatContainer(
    content=HSplit(
        [
            header_bar,
            header_field,
            white_separator,
            prompt_field,
            white_separator,
            bottom_window,
        ]
    ),
    floats=[
        Float(
            content=CompletionsMenu(max_height=6),
            xcursor=True,
            ycursor=True,
        )
    ],
)

layout = Layout(root_container, focused_element=prompt_field)

# ---------------------------------------------------------------------------
# Styles / Application
# ---------------------------------------------------------------------------
style = Style.from_dict({
    "frame.border": f"{MAIN_COLOR}",       # Outer frame borders
    "frame.label": f"bold {MAIN_COLOR}",    # Top border title
    "line": f"{MAIN_COLOR}",                # <--- Colors VerticalLine & HorizontalLine widgets
    "vertical-line": f"{MAIN_COLOR}",       # Fallback vertical line class token
    
    # Text styles
    "banner-welcome": "bold #ffffff",
    "accent-icon": f"{MAIN_COLOR}",
    "section-title": f"bold {MAIN_COLOR}",
    "banner-sub": "#a0a0a0",
    "body-text": "#d0d0d0",
    "code-text": "bold #ffffff",
    "sub-link": "italic #a0a0a0",
})

# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
app = Application(
    layout=layout,
    key_bindings=kb,
    style=style,
    full_screen=True,
    mouse_support=True,
)

def main() -> None:
    app.run()

if __name__ == "__main__":
    main()