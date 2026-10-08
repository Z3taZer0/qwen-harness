# Qwen Harness (`qh`)

A lean, fast agentic harness and desktop assistant tailored for **Qwen 3.8 27B** running on a
local or remote **vLLM** server. Small static toolset, prefix-cache-friendly prompts, and every
part of how it reasons is a plain file you can edit.

## Highlights

- **Two frontends, one agent core.** The agent emits structured events, and both UIs render them live.
  - **Desktop app (GTK4 / libadwaita):** a chat sidebar with search; streamed markdown with code
    blocks and copy buttons; collapsible thoughts; live tool cards (arguments, output, duration,
    status); and a stop button. It also has a multi-line composer that takes pasted or dropped
    images, a working-directory picker, a mode picker, a context gauge, a server status dot,
    approval dialogs, preferences, and desktop notifications when a turn finishes in the background.
  - **Terminal (rich + prompt_toolkit):** rendered markdown, live spinners with tok/s, streamed
    thoughts, compact tool lines with output previews, slash commands with tab completion,
    persistent input history, and a bottom status bar.
- **Interrupt anything.** Esc (GUI) or Ctrl+C (CLI) aborts generation. The connection is closed, so
  vLLM frees the GPU at once. It also kills running shell commands, including their child processes.
- **Saved conversations.** Every chat is saved to `~/.local/share/qh/sessions/`. Resume it from the
  sidebar, `/resume`, or `qh -c`.
- **Undo / retry.** Drop the last exchange, or regenerate the answer.
- **Cache-aligned prompts.** Switching mode or working directory mid-chat never rewrites the
  system prompt. The change rides on your next message, so the prefix cache survives.
- **Reasoning modes:** `auto` (adaptive), `complex`, `artistic`, `quick`, plus your own (see below).
- **Safety net for destructive commands.** `rm -rf`, `sudo`, `dd`, `mkfs`, `git push --force`… ask
  for approval first. You can turn this off.
- **Fast vision pipeline:** bounded-pixel JPEG downscaling, with old images evicted to protect the cache.
- **Tools:** shell, read/write/edit file, grep/find, web search, fetch, download, image
  inspect/view, skill loader, memory notes.

## Install

```bash
uv venv --python /usr/bin/python3.14 --system-site-packages .venv   # system site-packages for PyGObject
source .venv/bin/activate
uv pip install -e .
./scripts/install-desktop.sh      # optional: adds "Qwen Harness" to the app menu
```

## Terminal

```bash
qh                                  # interactive
qh "find a 4k wallpaper like my collection and set it"   # one-shot
qh -m quick "what's using port 8080?"
qh -i shot.png "what's wrong in this screenshot?"
journalctl -b -p err | qh "summarize these errors"       # stdin is appended
qh -c                               # continue the most recent chat
qh -l                               # list saved chats;  qh -r 3  resumes #3
```

Keys: **Enter** sends · **Alt+Enter** newline · **Ctrl+C** interrupts the turn (or clears the line)
· **Ctrl+D** quits · **Tab** completes commands, modes, paths and sessions.

| Command | |
|---|---|
| `/mode [id]` | show / switch reasoning mode |
| `/think on\|off\|auto` | override thinking for every mode |
| `/reasoning` | toggle streaming of thoughts |
| `/img <path> [text]` | send an image |
| `/new` | new conversation |
| `/sessions`, `/resume <n\|id>` | list / continue saved chats |
| `/undo`, `/retry` | drop the last exchange (its text goes back into the prompt) / ask again |
| `/compact`, `/context` | summarize history now / show context and cache stats |
| `/cwd [path]` | show / change the working directory |
| `/copy` | copy the last answer (wl-copy) |
| `/notes [approve\|clear]` | learned notes and pending suggestions |
| `/skills`, `/config` | list skills / show effective config |
| `!cmd` | run a shell command yourself; it is not sent to the model |

## Desktop app

`./qh-gui-launcher.sh`, `qh-gui`, or **Qwen Harness** in the app menu.

| Key | |
|---|---|
| Enter / Shift+Enter | send / new line |
| Esc | stop generation and running commands |
| Ctrl+N · Ctrl+R | new chat · retry last message |
| Ctrl+O · Ctrl+V | attach image · paste image from clipboard |
| ↑ in an empty input | bring back your last message |
| F9 · Ctrl+L · Ctrl+, | toggle chat list · focus input · preferences |
| Ctrl+Shift+K | compact context |

## Configuration

Settings come from `QH_*` environment variables, or from `~/.config/qh/config.env` (`KEY=value`
lines, `QH_` prefix optional). The GUI preferences dialog writes that file for you.

| Variable | Default | Description |
|---|---|---|
| `QH_BASE_URL` | `http://zeta-fixe:8080/v1` | vLLM OpenAI-compatible endpoint |
| `QH_MODEL` | `qwen3.8-27b` | Model name (falls back to the served model if it doesn't match) |
| `QH_MODE` | `auto` | Default reasoning mode |
| `QH_THINKING` | `auto` | `on` / `off` overrides every mode |
| `QH_CONTEXT_WINDOW` | `126976` | Must match vLLM `--max-model-len` |
| `QH_MAX_STEPS` | `0` | Model calls per message (`0` = unlimited) |
| `QH_MAX_TOKENS` | `8192` | Output tokens per call |
| `QH_CONFIRM_DANGEROUS` | `true` | Ask before destructive shell commands |
| `QH_SHOW_REASONING` | `true` | Stream thoughts (CLI) / expand them while thinking (GUI) |
| `QH_SAVE_SESSIONS` | `true` | Persist conversations |
| `QH_BASH_TIMEOUT` | `120` | Seconds before a shell command is killed |

See `src/qh/config.py` for the context-compaction and vision knobs.

## Customization

Everything lives in `~/.config/qh/`. Edits apply to the next new chat.

| Path | What it does |
|---|---|
| `system.md` | Replaces the built-in system prompt entirely |
| `profile.md` | Facts about you and your machines, sent once at the start of each chat |
| `skills/**/SKILL.md` | Playbooks. Only a one-line index sits in the prompt; the model loads a full skill on demand |
| `modes/*.md` | Custom reasoning modes, or overrides of the built-ins (same `id`) |
| `notes.md` | Lessons the agent proposed and you approved |
| `AGENTS.md` (in a project) | Per-project instructions, picked up from the working directory |

Example custom mode, saved as `~/.config/qh/modes/review.md`:

```markdown
---
id: review
name: Code Review
description: Read-only critical review of the current change
thinking: always          # always | adaptive | initial_only | off
temperature: 0.5
---
Reasoning Pattern (Review):
- Read the diff first (git diff), then only the code it touches.
- Look for behaviour changes, edge cases and missing tests; never edit files.
- Report findings ranked by severity, each with file:line.
```

Destructive-command patterns are listed in `DANGEROUS` in `src/qh/tools.py`.
