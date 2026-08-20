"""Formatting helpers for the full-screen TUI.

No Rich Console/Live/Panel here anymore: once ``Application(full_screen=True)``
owns the terminal, Rich writing to stdout directly would corrupt the
prompt_toolkit-drawn screen. Instead these functions return plain strings
that ``cli.py`` appends into the scrollable ``header_field`` TextArea.
"""

from __future__ import annotations

import time

ICON = "✻"


def format_status_block(model: str, effort: str, cwd: str) -> str:
    """Text for the top of the output pane, shown once at startup."""
    rule = "─" * 60
    return f"{rule}\n {ICON} {model} with {effort} effort  ·  cwd: {cwd}\n{rule}\n\n"


def format_status_line(model: str, effort: str, cwd: str) -> str:
    """Single-line status text for the full-width header bar Window.

    Unlike ``format_status_block`` (a fixed-width text rule meant for a
    scrollback pane), this has no baked-in width — the Window it's placed
    in is styled with a background color and spans the terminal naturally,
    so it never looks truncated on wide terminals.
    """
    return f"{model} with {effort} effort  ·  cwd: {cwd}"


def format_error(message: str) -> str:
    return f"\n⚠  {message}\n\n"


class StreamRenderer:
    """Tracks a streaming LLM turn and emits text *deltas* via a callback.

    Unlike the old Rich-based ``ResponseRenderer`` (which held the full
    accumulated string and re-rendered a ``Live`` display), this pushes
    incremental chunks to ``on_update`` so the caller can append them to a
    TextArea cheaply, from any thread (the callback is expected to be
    thread-safe — ``cli.py`` wires it through ``call_soon_threadsafe``).

    Usage::

        renderer = StreamRenderer(on_update=append_to_ui)
        renderer.begin_thinking()
        renderer.begin_content()       # emits "Thought for Ns" once
        renderer.append(token)         # emits each visible token
        ui.append(renderer.footer(turn_start))  # "Crunched for Xs"
    """

    def __init__(self, on_update) -> None:
        self._on_update = on_update
        self._thinking_start: float | None = None
        self._content: list[str] = []

    def begin_thinking(self) -> None:
        if self._thinking_start is None:
            self._thinking_start = time.monotonic()

    def begin_content(self) -> None:
        if self._thinking_start is not None:
            elapsed = int(time.monotonic() - self._thinking_start)
            self._on_update(f"{ICON} Thought for {elapsed}s\n\n")
            self._thinking_start = None  # only emit once

    def append(self, token: str) -> None:
        self._content.append(token)
        self._on_update(token)

    def footer(self, turn_start: float) -> str:
        """Text for the "Crunched for Xs" line — append this after streaming ends."""
        elapsed = round(time.monotonic() - turn_start, 1)
        return f"\n\n{ICON} Crunched for {elapsed}s\n\n"

    @property
    def text(self) -> str:
        """All accumulated visible content (for conversation history)."""
        return "".join(self._content)