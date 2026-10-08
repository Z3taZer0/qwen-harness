"""Markdown helpers shared by the frontends.

Both UIs stream markdown progressively: completed blocks are rendered once and never
touched again, only the trailing (still growing) block is re-rendered.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

FENCE = re.compile(r"^\s*(```|~~~)")


def split_blocks(text: str) -> tuple[list[str], str]:
    """Split into (complete blocks, unfinished tail). Blocks end at a blank line outside
    a code fence, or at a closing fence."""
    blocks, cur, fence = [], [], None
    lines = text.split("\n")
    tail_line = lines.pop()  # the last line is never complete while streaming
    for line in lines:
        m = FENCE.match(line)
        if fence:
            cur.append(line)
            if m and m.group(1) == fence and line.strip() == fence:
                blocks.append("\n".join(cur))
                cur, fence = [], None
            continue
        if m:
            if cur:
                blocks.append("\n".join(cur))
            cur, fence = [line], m.group(1)
            continue
        if not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    rest = "\n".join(cur + [tail_line]) if cur or tail_line else ""
    return blocks, rest


def all_blocks(text: str) -> list[str]:
    blocks, rest = split_blocks(text)
    return blocks + ([rest] if rest.strip() else [])


# ---------------------------------------------------------------- Pango (GTK)
@dataclass
class Block:
    kind: str          # text | code | table | hr
    body: str          # pango markup for text, raw text for code/table
    lang: str = ""


_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITAL = re.compile(r"(?<![\w*])(\*|_)(?=\S)(.+?)(?<=\S)\1(?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_URL = re.compile(r"(?<![\"'>=])\b(https?://[^\s<]+[^\s<.,;:!?)\]'\"])")


def inline(text: str, code_attrs: str = "") -> str:
    """Inline markdown -> Pango markup (escaping everything else)."""
    slots: list[str] = []

    def keep(s: str) -> str:
        slots.append(s)
        return f"\x00{len(slots) - 1}\x00"

    text = _INLINE_CODE.sub(lambda m: keep(
        f'<span font_family="monospace"{(" " + code_attrs) if code_attrs else ""}>{html.escape(m.group(1), quote=False)}</span>'), text)
    text = _LINK.sub(lambda m: keep(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1), quote=False)}</a>'), text)
    text = _URL.sub(lambda m: keep(f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1), quote=False)}</a>'), text)
    text = html.escape(text, quote=False)
    text = _BOLD.sub(r"<b>\2</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITAL.sub(r"<i>\2</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], text)


_HEAD_SIZES = {1: "x-large", 2: "large", 3: "medium"}


def to_pango(block: str, code_attrs: str = "", dim_color: str = "") -> Block:
    lines = block.split("\n")
    m = FENCE.match(lines[0])
    if m:
        lang = lines[0].strip()[3:].strip()
        body = lines[1:]
        if body and body[-1].strip() == m.group(1):
            body = body[:-1]
        return Block("code", "\n".join(body), lang)
    if re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", block):
        return Block("hr", "")
    if all(l.strip().startswith("|") for l in lines if l.strip()) and len(lines) >= 2:
        return Block("table", format_table(lines))
    out = []
    for line in lines:
        if h := re.match(r"^(#{1,6})\s+(.*)$", line):
            size = _HEAD_SIZES.get(len(h.group(1)), "medium")
            out.append(f'<span size="{size}" weight="bold">{inline(h.group(2), code_attrs)}</span>')
        elif li := re.match(r"^(\s*)([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)$", line):
            indent = "    " * (len(li.group(1).expandtabs(2)) // 2)
            bullet = li.group(2) if li.group(2)[0].isdigit() else "•"
            if li.group(3):
                bullet = "☑" if li.group(3).strip().lower() == "[x]" else "☐"
            out.append(f"{indent}{bullet} {inline(li.group(4), code_attrs)}")
        elif q := re.match(r"^\s*>\s?(.*)$", line):
            col = f' foreground="{dim_color}"' if dim_color else ""
            out.append(f"<span{col}><i>┃ {inline(q.group(1), code_attrs)}</i></span>")
        else:
            out.append(inline(line, code_attrs))
    return Block("text", "\n".join(out))


def format_table(lines: list[str]) -> str:
    rows = []
    for l in lines:
        l = l.strip()
        if not l:
            continue
        cells = [c.strip() for c in l.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            rows.append(None)  # separator
            continue
        rows.append([re.sub(r"\*\*|`", "", c) for c in cells])
    n = max((len(r) for r in rows if r), default=0)
    widths = [max((len(r[i]) for r in rows if r and i < len(r)), default=0) for i in range(n)]
    out = []
    for r in rows:
        if r is None:
            out.append("─┼─".join("─" * w for w in widths))
        else:
            out.append(" │ ".join((r[i] if i < len(r) else "").ljust(widths[i]) for i in range(n)))
    return "\n".join(out)
