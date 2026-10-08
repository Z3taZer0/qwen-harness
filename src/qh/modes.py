"""Task-specific Reasoning Modes for Qwen 3.8.
Instead of a simple numeric effort/tokens toggle, reasoning modes define specific cognitive patterns:
- how the agent approaches the problem in its thoughts
- what criteria it weighs
- when and how intensely thinking is enabled per step.

Custom modes: drop a markdown file in ~/.config/qh/modes/, e.g. `review.md`:

    ---
    id: review
    name: Code Review
    description: Read-only critical review of a change
    thinking: always            # always | adaptive | initial_only | off
    temperature: 0.6
    top_p: 0.95
    ---
    Reasoning Pattern (Review):
    - ...

The body becomes the mode's system guidance. A file whose id matches a built-in mode
replaces it, so the built-ins below can be tuned without touching the code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .config import CONFIG_DIR

MODES_DIR = CONFIG_DIR / "modes"
POLICIES = ("always", "adaptive", "initial_only", "off")

@dataclass
class ReasoningMode:
    id: str
    name: str
    description: str
    thinking_policy: Literal["always", "adaptive", "initial_only", "off"]
    system_guidance: str
    temperature: float = 0.6
    top_p: float = 0.95

MODES: dict[str, ReasoningMode] = {
    "auto": ReasoningMode(
        id="auto",
        name="Adaptive (Default)",
        description="Balanced reasoning enabled on the first turn and upon errors; executes tools directly when confident.",
        thinking_policy="adaptive",
        system_guidance="""Reasoning Pattern (Adaptive):
- Formulate a clear, direct plan on the initial step.
- Focus on efficient tool execution without unnecessary overthinking.
- When an unexpected error occurs, deeply analyze root cause before retry.""",
        temperature=0.6,
        top_p=0.95,
    ),
    "complex": ReasoningMode(
        id="complex",
        name="Complex / Deep Analysis",
        description="Rigorous multi-step reasoning on every turn: structural proofs, invariants, edge cases, and test strategies.",
        thinking_policy="always",
        system_guidance="""Reasoning Pattern (Complex & Formal):
- Deeply decompose the request before taking actions.
- Formulate hypothesis, inspect architecture, state preconditions, invariants, and edge cases.
- Before editing or running critical commands, anticipate failure modes and downstream effects.
- Verify every outcome through rigorous empirical steps (tests, linters, checks).""",
        temperature=0.6,
        top_p=0.95,
    ),
    "artistic": ReasoningMode(
        id="artistic",
        name="Artistic / Aesthetic Exploration",
        description="Aesthetic evaluation: composition, color temperature, atmospheric lighting, mood consistency, and visual cleanliness.",
        thinking_policy="adaptive",
        system_guidance="""Reasoning Pattern (Aesthetic & Visual Judgement):
- When evaluating visual assets (wallpapers, artwork, designs), avoid rigid mechanical parsing.
- Evaluate composition, visual harmony, lighting, color palettes (warm vs cold, contrast), mood, and atmospheric emotion.
- Prioritize cleanliness: reject watermarks, site/uploader logos, signatures stamped over the art, subtitles,
  UI/screenshot overlays, artifacts and compression noise.
- Text that belongs to the artwork is fine: a character's name, a title logo, stylized typography or
  small credits that are part of the composition. Don't discard an image just because it contains text;
  judge whether the text is part of the design or an overlay added on top of it.
- Compare candidate choices side-by-side against the user's stylistic tastes before committing.""",
        temperature=0.7,
        top_p=0.95,
    ),
    "quick": ReasoningMode(
        id="quick",
        name="Fast Action",
        description="Minimal latency with thinking disabled. Ideal for quick shell commands, lookups, and fast file tweaks.",
        thinking_policy="off",
        system_guidance="""Reasoning Pattern (Fast Action):
- Act immediately with zero preamble.
- Execute tools directly and deliver the concise result.""",
        temperature=0.7,
        top_p=0.8,
    ),
}

def _load_custom() -> None:
    if not MODES_DIR.is_dir():
        return
    for f in sorted(MODES_DIR.glob("*.md")):
        try:
            text = f.read_text()
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        meta, body = ({}, text) if not m else (dict(
            (k.strip(), v.strip().strip("\"'")) for k, v in
            (line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
        ), m.group(2))
        mid = meta.get("id") or f.stem
        base = MODES.get(mid)
        policy = meta.get("thinking", base.thinking_policy if base else "adaptive").split("#")[0].strip()
        try:
            MODES[mid] = ReasoningMode(
                id=mid,
                name=meta.get("name") or (base.name if base else mid.title()),
                description=meta.get("description") or (base.description if base else ""),
                thinking_policy=policy if policy in POLICIES else "adaptive",
                system_guidance=body.strip() or (base.system_guidance if base else ""),
                temperature=float(meta.get("temperature", base.temperature if base else 0.6)),
                top_p=float(meta.get("top_p", base.top_p if base else 0.95)),
            )
        except ValueError:
            continue


_load_custom()


def get_mode(mode_id: str) -> ReasoningMode:
    return MODES.get(mode_id, MODES["auto"])
