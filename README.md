# Qwen Harness (`qh`)

A high-performance agentic harness and desktop assistant tailored for **Qwen 3.8 27B** running on a local or remote **vLLM** inference server.

## Highlights

- **Cache-Aligned Prompt Engine**: Preserves prefix-cache boundaries by keeping reasoning outputs contiguous, using static tool definitions, and preventing prompt thrashing.
- **Dynamic Reasoning Modes**: Task-specific cognitive patterns:
  - `Adaptive` (`auto`): Balances thinking on initial turn and error recovery.
  - `Complex` (`complex`): Enforces deep structural decomposition and formal invariants.
  - `Artistic` (`artistic`): Focuses on visual harmony, lighting, composition, and cleanliness.
  - `Fast` (`quick`): Zero preamble, instant tool execution.
- **Fast Vision Pipeline**: Downscales image payloads with fast bilinear filtering and JPEG encoding to prevent GPU prefill latency and eliminate CPU bottlenecks.
- **Dual Interface**:
  - **Modern GTK4 / Libadwaita Desktop App** integrated with Wayland/Serpantinum.
  - **Lean CLI** with interactive streaming and token metrics.
- **Autonomous Toolset**: Built-in shell runner, file editor, grep/find, web search, web fetch/download, fast image inspection (`identify`), and skill playbook loader.

## Quick Start

### Installation

```bash
uv venv --python /usr/bin/python3.14 --system-site-packages .venv
source .venv/bin/activate
uv pip install -e .
```

### Run CLI

```bash
# Interactive mode
qh

# Single query
qh "Find and download a 4k anime wallpaper similar to my collection"
```

### Launch Desktop App

```bash
./qh-gui-launcher.sh
```
Or launch **Qwen Harness** from your Serpantinum applications menu.

## Configuration

Set environment variables in your shell or `~/.config/qh/config.env`:

| Variable | Default | Description |
|---|---|---|
| `QH_BASE_URL` | `http://zeta-fixe:8080/v1` | vLLM OpenAI-compatible endpoint |
| `QH_MODEL` | `qwen3.8-27b` | Model name |
| `QH_MODE` | `auto` | Default reasoning mode (`auto`, `complex`, `artistic`, `quick`) |
| `QH_CONTEXT_WINDOW` | `126976` | Total context window |
| `QH_MAX_STEPS` | `0` | Turn budget per user turn (`0` = unlimited) |
