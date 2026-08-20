# Build: Claude-Code-Style Python CLI Agent

## Objective
Build a minimal Python CLI chat agent that visually and interactively mimics
Claude Code's terminal UI, streams responses from an LLM, and reads its
configuration (model, reasoning effort, endpoint) from environment variables.

## Tech Stack
- Python 3.11+
- `rich` — panels, live-updating text, spinners, colors
- `prompt_toolkit` — the `❯` input prompt (history, multiline via Alt+Enter,
  arrow-key editing, Ctrl+C to cancel a turn without exiting)
- `python-dotenv` — load `.env`
- `litellm` — model calls with streaming, so it works against both an
  Anthropic-compatible endpoint and a local LiteLLM gateway (Ollama, etc.)

## .env Configuration
MODEL=claude-sonnet-4-6
EFFORT=high # low | medium | high
API_BASE=http://localhost:4000 # optional, e.g. LiteLLM proxy
API_KEY=sk-...


Fail fast with a clear, colored error message if `MODEL` or `API_KEY` is missing.
## UI Layout

### 1. Top status panel (rendered once at startup)
A rounded `rich.Panel` showing:
- A small custom icon in Claude-Code style: a colored unicode glyph such as
  `✻` (or design a similar asterisk/star-burst symbol) in orange/red
- Model name + effort level
- Current working directory (the folder the script was launched from)

╭───────────────────────────────────────────╮
│ ✻ claude-sonnet-4-6 · effort: high │
│ cwd: /home/miguel/projects/agent-demo │
╰───────────────────────────────────────────╯


### 2. Prompt line
`❯ ` prompt via `prompt_toolkit`, with:
- persistent command history (stored in a local dotfile)
- multiline input support
- `Ctrl+C` cancels the in-flight request only (returns to prompt)
- `Ctrl+D`, `/exit`, or `/quit` exits the app
- `/clear` resets conversation history
- `/help` lists commands

### 3. Turn rendering (must match this exact style)

❯ hello
Thought for 13s
Hello! How can I help you today? 😊
✻ Crunched for 13s

Behavior:
1. Echo `❯ {user input}`
2. If the model streams reasoning/thinking tokens, show a live, animated
   `  Thought for {n}s` line (updating in place) while thinking is in
   progress; freeze it at the final time once thinking ends
3. Stream the visible answer as plain left-aligned text, token by token,
   no markdown box
4. After completion, print a dim footer line: `✻ Crunched for {n}s`
   (total wall-clock time for the turn — thinking + generation)

## Functional Requirements
- Maintain in-memory multi-turn conversation history (list of role/content
  messages)
- Use `litellm.completion(..., stream=True)` and separate thinking deltas
  from content deltas if the provider/model exposes them
- Handle connection errors (e.g. LiteLLM/Ollama unreachable) and API errors
  gracefully — print a red inline message, never crash the REPL loop
- Should work identically whether MODEL points to a hosted Claude model or
  a local Ollama model routed through LiteLLM

## Non-goals (keep it simple)
- No tool-calling, file editing, or bash execution — pure chat only
- No persistent history across restarts
- No full multi-pane TUI (no Textual) — single scrolling terminal output

## Deliverables
- `agent.py` or small package (`agent/cli.py`, `agent/ui.py`, `agent/config.py`)
- `requirements.txt` with pinned versions
- `.env.example`
- `README.md` with quickstart (venv, pip install, cp .env.example .env, run)
