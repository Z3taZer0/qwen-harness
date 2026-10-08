"""Central configuration.

Precedence (highest first): QH_<NAME> env var > ~/.config/qh/config.env > defaults.
config.env is plain `KEY=value` lines (QH_ prefix optional, `#` comments allowed).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "qh"
CONFIG_ENV = CONFIG_DIR / "config.env"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "qh"


@dataclass
class Config:
    base_url: str = "http://zeta-fixe:8080/v1"
    model: str = "qwen3.8-27b"
    api_key: str = "none"

    # --- context management -------------------------------------------------
    context_window: int = 126_976      # must match vLLM --max-model-len
    compact_at: float = 0.70           # start compaction at 70% of the window (~89k)
    compact_target: float = 0.40       # ...and get down to ~40% (~50k)
    tail_fraction: float = 0.15        # recent history kept verbatim when summarizing
    keep_recent_tool_results: int = 6  # newer tool results are never pruned
    max_tool_chars: int = 10_000       # hard cap on any single tool output
    chars_per_token: float = 3.3       # only for estimating *new* messages

    # --- generation & reasoning mode ----------------------------------------
    mode: str = "auto"                 # auto | complex | artistic | quick
    thinking: str = "auto"             # global override: on | off | auto (= follow the mode)
    max_tokens: int = 8192
    max_steps: int = 0                 # model calls per user request, 0 = unlimited

    # --- vision (server: --mm-processor-kwargs min 65536 / max 2097152 px) ---
    image_max_pixels: int = 360_000    # ~450 visual tokens (fast inference)
    image_min_pixels: int = 65_536     # matches server min window
    max_images_in_context: int = 7     # server allows 8; evicting earlier images busts the prefix cache

    # --- tools / safety -----------------------------------------------------
    bash_timeout: int = 120
    confirm_dangerous: bool = False    # ask before rm -rf, sudo, dd, mkfs, shutdown, ...

    # --- desktop ------------------------------------------------------------
    wallpaper_dir: str = "~/Pictures/Wallpapers"
    wallpaper_cmd: str = ""            # e.g. "serpantinum wallpaper {path}"; empty = auto-detect

    # --- interface ----------------------------------------------------------
    show_reasoning: bool = True        # stream the model's thoughts (CLI: dim text, GUI: expander)
    save_sessions: bool = True         # persist conversations to ~/.local/share/qh/sessions

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        file_vals = read_env_file(CONFIG_ENV)
        for f in fields(cls):
            key = f"QH_{f.name.upper()}"
            v = os.environ.get(key, file_vals.get(key))
            if v is not None:
                try:
                    setattr(cfg, f.name, _coerce(getattr(cfg, f.name), v))
                except ValueError:
                    pass  # a typo in config.env must not prevent startup
        return cfg

    def save(self, names: list[str]) -> None:
        """Persist selected fields to config.env, keeping other lines and comments intact."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = CONFIG_ENV.read_text().splitlines() if CONFIG_ENV.exists() else []
        todo = {f"QH_{n.upper()}": _fmt(getattr(self, n)) for n in names}
        out = []
        for line in lines:
            k = line.split("=", 1)[0].strip()
            k = k if k.startswith("QH_") else f"QH_{k}"
            if "=" in line and not line.lstrip().startswith("#") and k in todo:
                out.append(f"{k}={todo.pop(k)}")
            else:
                out.append(line)
        out += [f"{k}={v}" for k, v in todo.items()]
        CONFIG_ENV.write_text("\n".join(out) + "\n")


def read_env_file(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return vals
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        k = k if k.startswith("QH_") else f"QH_{k.upper()}"
        vals[k] = v.strip().strip("\"'")
    return vals


def _coerce(default, v: str):
    if isinstance(default, bool):
        if v.strip().lower() in ("1", "true", "yes", "on"):
            return True
        if v.strip().lower() in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(v)
    return type(default)(v)


def _fmt(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)
