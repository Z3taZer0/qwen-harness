"""Task-specific Reasoning Modes for Qwen 3.8.
Instead of a simple numeric effort/tokens toggle, reasoning modes define specific cognitive patterns:
- how the agent approaches the problem in its thoughts
- what criteria it weighs
- when and how intensely thinking is enabled per step.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

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
- Prioritize cleanliness: verify absence of watermarks, artifacts, distracting text/subtitles, or compression noise.
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

def get_mode(mode_id: str) -> ReasoningMode:
    return MODES.get(mode_id, MODES["auto"])
