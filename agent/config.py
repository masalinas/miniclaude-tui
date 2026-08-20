"""Configuration loading from environment / .env file."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env from the project root (directory containing this package)
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

# Effort budget for Claude thinking, in tokens.  The "medium" default
_EFFORT_BUDGET: dict[str, int] = {
    "low": 2_048,
    "medium": 8_192,
    "high": 16_384,
}

_VALID_EFFORTS = tuple(_EFFORT_BUDGET.keys())


@dataclass(frozen=True)
class Config:
    """Runtime settings read from the environment."""

    model: str = ""
    api_key: str = ""
    effort: str = "medium"
    api_base: Optional[str] = None

    # -- validation ---------------------------------------------------------

    @classmethod
    def from_env(cls) -> "Config":
        """Construct from environment variables, failing fast on issues."""
        model = os.environ.get("MODEL", "").strip()
        api_key = os.environ.get("API_KEY", "").strip()
        raw_effort = os.environ.get("EFFORT", "medium").strip().lower()
        api_base = os.environ.get("API_BASE", "").strip() or None

        missing: list[str] = []
        if not model:
            missing.append("MODEL")
        if not api_key:
            missing.append("API_KEY")

        if missing:
            names = " and ".join(f"`{m}`" for m in missing)
            print(
                f"\n[bold red]Error:[/bold red] Missing required environment variable(s): {names}\n"
                f"  Add them to your [.env] file or export them, then restart.\n",
                file=sys.stderr,
            )
            sys.exit(1)

        if raw_effort not in _VALID_EFFORTS:
            print(
                f"\n[bold yellow]Warning:[/bold yellow] Invalid EFFORT={raw_effort!r}, "
                f"falling back to 'medium'.\n",
                file=sys.stderr,
            )
            raw_effort = "medium"

        return cls(
            model=model,
            api_key=api_key,
            effort=raw_effort,
            api_base=api_base,
        )

    # -- helpers ------------------------------------------------------------

    def completion_kwargs(self) -> dict[str, object]:
        """Keyword arguments for ``litellm.completion``.

        LiteLLM auto-detects the provider from the model name
        (e.g. ``claude-sonnet-4-6`` → Anthropic).  We only need to:
        - pass api_key and optionally api_base (for a proxy / local gateway)
        - enable streaming
        - attach extended-thinking budget for Claude models via extra_body

        When using the native ``openrouter/`` prefix, we omit ``api_base``
        because LiteLLM resolves the URL internally; passing both breaks.
        """
        kwargs: dict[str, object] = {
            "api_key": self.api_key,
            "stream": True,
        }

        # Only pass api_base for non-OpenRouter proxies — when using the
        # native "openrouter/" prefix LiteLLM resolves the URL itself.
        if self.api_base and "openrouter" not in self.api_base:
            kwargs["api_base"] = self.api_base

        # Claude extended thinking — only when model looks like a Claude model
        # LiteLLM routes claude-* names to the Anthropic provider.
        if self.model.startswith("claude"):
            kwargs["extra_body"] = {
                "thinking": {
                    "type": "text",
                    "budget_tokens": _EFFORT_BUDGET[self.effort],
                }
            }

        return kwargs
