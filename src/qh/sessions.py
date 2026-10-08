"""Conversation persistence: one JSON file per session in ~/.local/share/qh/sessions.

Messages are stored exactly as sent (plus harness-private `_` keys), so a resumed
session continues with the same history the model saw.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR

SESSIONS_DIR = DATA_DIR / "sessions"


@dataclass
class SessionInfo:
    id: str
    title: str
    updated: float
    cwd: str
    mode: str
    turns: int
    path: Path

    @property
    def when(self) -> str:
        d = time.time() - self.updated
        if d < 60:
            return "just now"
        if d < 3600:
            return f"{int(d // 60)} min ago"
        if d < 86400:
            return f"{int(d // 3600)} h ago"
        if d < 7 * 86400:
            return f"{int(d // 86400)} d ago"
        return time.strftime("%Y-%m-%d", time.localtime(self.updated))


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def title_from(text: str) -> str:
    t = " ".join(text.split())
    return t if len(t) <= 60 else t[:57].rstrip() + "…"


def save(sid: str, data: dict) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    p = SESSIONS_DIR / f"{sid}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, p)  # atomic: a crash never leaves a half-written session
    # Small sidecar so listing sessions never has to parse whole histories.
    meta = {k: v for k, v in data.items() if k != "messages"}
    (SESSIONS_DIR / f"{sid}.meta").write_text(json.dumps(meta, ensure_ascii=False))


def load(sid: str) -> dict:
    return json.loads((SESSIONS_DIR / f"{sid}.json").read_text())


def delete(sid: str) -> None:
    (SESSIONS_DIR / f"{sid}.json").unlink(missing_ok=True)
    (SESSIONS_DIR / f"{sid}.meta").unlink(missing_ok=True)


def rename(sid: str, title: str) -> None:
    d = load(sid)
    d["title"] = title
    save(sid, d)


def list_sessions(limit: int = 200) -> list[SessionInfo]:
    if not SESSIONS_DIR.is_dir():
        return []
    files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        meta = p.with_suffix(".meta")
        try:
            d = json.loads((meta if meta.exists() else p).read_text())
        except (OSError, ValueError):
            continue
        out.append(SessionInfo(
            id=p.stem, title=d.get("title") or "(untitled)", updated=d.get("updated") or p.stat().st_mtime,
            cwd=d.get("cwd", ""), mode=d.get("mode", ""), turns=d.get("turns", 0), path=p,
        ))
    return out


def resolve(ref: str) -> str | None:
    """Session id from an id, an id prefix, or a 1-based index into the recent list."""
    items = list_sessions()
    if ref.isdigit() and 0 < int(ref) <= len(items) and len(ref) < 4:
        return items[int(ref) - 1].id
    for s in items:
        if s.id == ref or s.id.startswith(ref):
            return s.id
    return None
