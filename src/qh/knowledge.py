"""Hints & knowledge, layered by cost:

  skills   - SKILL.md playbooks. Only a one-line index is in the system prompt; the full
             text is loaded on demand with load_skill().
             Uses ~/.config/qh/skills as native location (no hermes dependency).
  profile  - facts about the user/machines, injected once in the first user message.
  AGENTS.md- per-project instructions, auto-loaded from the working directory.
  notes    - lessons the agent proposed and the user approved (notes.md, size-capped).

Everything is plain markdown the user can edit. All of it is resolved once at session
start, so the prompt prefix stays byte-stable (prefix-cache friendly).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
CFG_DIR = HOME / ".config" / "qh"
SKILL_DIRS = [CFG_DIR / "skills"]
PROFILE_FILES = [CFG_DIR / "profile.md"]
NOTES = CFG_DIR / "notes.md"
NOTES_PENDING = CFG_DIR / "notes.pending.md"
MAX_PROFILE, MAX_NOTES, MAX_AGENTS = 3000, 2000, 4000


@dataclass
class Skill:
    name: str
    description: str
    path: Path


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if kv and not line.startswith(" "):
            key, val = kv.group(1), kv.group(2).strip()
            out[key] = "" if val in (">", "|", ">-", "|-") else val.strip("\"'")
        elif key and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def discover_skills() -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for root in SKILL_DIRS:
        if not root.is_dir():
            continue
        files = sorted(Path(dp) / fn for dp, _, fns in os.walk(root, followlinks=True) for fn in fns if fn.endswith(".md"))
        for f in files:
            try:
                fm = _frontmatter(f.read_text(errors="replace")[:2000])
            except OSError:
                continue
            if fm.get("name") and fm["name"] not in skills:
                skills[fm["name"]] = Skill(fm["name"], fm.get("description", ""), f)
    return skills


def skill_index(skills: dict[str, Skill]) -> str:
    if not skills:
        return ""
    lines = [f"- {s.name}: {re.sub(r'\\s+', ' ', s.description)[:170]}" for s in skills.values()]
    return ("\n\nSkills (playbooks with hard-won details). If a task matches one, call load_skill(name) "
            "BEFORE starting:\n" + "\n".join(lines))


def load_skill(skills: dict[str, Skill], name: str) -> str:
    s = skills.get(name)
    if not s:
        return f"Error: unknown skill '{name}'. Available: {', '.join(skills)}"
    text = re.sub(r"^---\n.*?\n---\n", "", s.path.read_text(errors="replace"), count=1, flags=re.S)
    extra = [p.name for p in s.path.parent.iterdir() if p.is_file() and p != s.path]
    tail = f"\n\n[Related files in {s.path.parent}: {', '.join(extra)} - read_file them if needed]" if extra else ""
    return text.strip() + tail


def _read(p: Path, cap: int) -> str:
    try:
        return p.read_text(errors="replace").strip()[:cap]
    except OSError:
        return ""


def session_context(cwd: Path) -> str:
    """Dynamic-but-session-stable facts for the first user message."""
    parts = []
    prof = "\n".join(t for p in PROFILE_FILES if (t := _read(p, MAX_PROFILE)))
    if prof:
        parts.append(f"[user profile]\n{prof.replace('§', '')}")
    for d in (cwd, *cwd.parents):
        if (t := _read(d / "AGENTS.md", MAX_AGENTS)):
            parts.append(f"[project instructions: {d}/AGENTS.md]\n{t}")
            break
    notes = _read(NOTES, 10**6)
    if notes:
        parts.append(f"[learned notes]\n{notes[-MAX_NOTES:]}")
    return "\n\n".join(parts)


def propose_note(note: str, ask) -> str:
    """User-approved memory. `ask(prompt) -> bool` is None when there is no TTY."""
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    note = note.strip().replace("\n", " ")
    if ask is not None and ask(f"Save note? \"{note}\""):
        with open(NOTES, "a") as f:
            f.write(f"- {note}\n")
        return "Note saved."
    with open(NOTES_PENDING, "a") as f:
        f.write(f"- {note}\n")
    return "Note not saved now; queued in notes.pending.md for the user to review."
