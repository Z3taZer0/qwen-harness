"""Central configuration. Every field can be overridden with a QH_<NAME> env var."""
from __future__ import annotations

import os
from dataclasses import dataclass, fields


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
    thinking: str = "auto"             # legacy override if specified (on | off | auto)
    max_tokens: int = 8192
    max_steps: int = 0                # 0 = unlimited steps per request                # model calls per user request

    # --- vision (server: --mm-processor-kwargs min 65536 / max 2097152 px) ---
    image_max_pixels: int = 360_000    # ~450 visual tokens (fast inference)  # ~1300 visual tokens
    image_min_pixels: int = 65_536     # matches server min window
    max_images_in_context: int = 7     # server allows 8; evicting earlier images busts the prefix cache

    bash_timeout: int = 120

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        for f in fields(cls):
            v = os.environ.get(f"QH_{f.name.upper()}")
            if v is not None:
                setattr(cfg, f.name, type(getattr(cfg, f.name))(v))
        return cfg
