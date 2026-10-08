"""Small, fixed tool set. Schemas are static and terse so the cached prefix never changes
and costs only a few hundred tokens (vs ~10k+ for general-purpose harnesses)."""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from . import knowledge, web


def clip(text: str, limit: int) -> str:
    """Head+tail truncation: errors and summaries usually live at the end."""
    if len(text) <= limit:
        return text
    h, t = int(limit * 0.4), int(limit * 0.6)
    return f"{text[:h]}\n... [{len(text) - h - t} chars truncated] ...\n{text[-t:]}"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]
    read_only: bool = False

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


S, I = {"type": "string"}, {"type": "integer"}

# Commands that need an explicit yes when cfg.confirm_dangerous is on. Edit to taste.
DANGEROUS = [
    (r"\brm\s+(-[^\s]*[rRf][^\s]*\s+)+", "recursive/forced delete"),
    (r"(^|[;&|]\s*|\s)(sudo|doas|pkexec)\s", "runs as root"),
    (r"\bdd\s+.*\bof=", "raw disk write"),
    (r"\bmkfs(\.\w+)?\b|\bwipefs\b|\bfdisk\b|\bparted\b", "filesystem/partition change"),
    (r"\b(shutdown|reboot|poweroff|halt)\b|systemctl\s+(poweroff|reboot|suspend|hibernate)", "power state"),
    (r">\s*/dev/(sd|nvme|vd|mmcblk)", "raw disk write"),
    (r"\bgit\s+(push\s+.*(--force|-f\b)|reset\s+--hard|clean\s+-\w*f)", "destructive git"),
    (r"\b(chmod|chown)\s+-R\s+\S+\s+/(\s|$)", "recursive permission change on /"),
    (r":\(\)\s*\{", "fork bomb"),
]


def _wallpaper_setters() -> list[str]:
    """Known Wayland/X11 wallpaper setters that are installed, best first."""
    has = shutil.which
    out = []
    if has("swww"):
        out.append("swww img {path} --transition-type fade --transition-duration 1")
    if has("awww"):
        out.append("awww img {path}")
    if has("hyprctl") and subprocess.run("pgrep -x hyprpaper", shell=True, capture_output=True).returncode == 0:
        out.append("hyprctl hyprpaper reload ,{path}")
    if has("swaybg"):
        out.append("pkill -x swaybg; (setsid swaybg -m fill -i {path} >/dev/null 2>&1 &)")
    if has("plasma-apply-wallpaperimage"):
        out.append("plasma-apply-wallpaperimage {path}")
    if has("gsettings") and os.environ.get("XDG_CURRENT_DESKTOP", "").lower().find("gnome") >= 0:
        out.append("gsettings set org.gnome.desktop.background picture-uri-dark file://{path} && "
                   "gsettings set org.gnome.desktop.background picture-uri file://{path}")
    if has("feh") and os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        out.append("feh --bg-fill {path}")
    return out


def danger(command: str) -> str | None:
    for rx, why in DANGEROUS:
        if re.search(rx, command):
            return why
    return None


class Toolbox:
    def __init__(self, cfg: Config, cwd: str):
        self.cfg = cfg
        self.cwd = Path(cwd).resolve()
        self.pending_images: list[dict] = []  # image parts to inject after tool results
        self.read_files: set[str] = set()
        self.skills = knowledge.discover_skills()
        self.ask = None  # set by the frontend: callable(str)->bool for user approval
        self.stop = threading.Event()  # set to abort a running bash command
        self.tools: dict[str, Tool] = {t.name: t for t in self._build()}

    # ---------------------------------------------------------------- helpers
    def path(self, p: str) -> Path:
        q = Path(os.path.expanduser(p))
        return q if q.is_absolute() else self.cwd / q

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        """Returns (output, is_error)."""
        tool = self.tools.get(name)
        if not tool:
            return f"Unknown tool '{name}'. Available: {', '.join(self.tools)}", True
        try:
            out = tool.fn(**args)
            limit = 24_000 if name == "load_skill" else self.cfg.max_tool_chars
            return clip(out, limit), out.startswith("Error")
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}", True
        except Exception as e:  # tool bugs must never kill the loop
            return f"Error: {type(e).__name__}: {e}", True

    # ------------------------------------------------------------------ tools
    def bash(self, command: str, timeout: int | None = None) -> str:
        if self.cfg.confirm_dangerous and (why := danger(command)):
            if self.ask is None:
                return f"Error: command needs user approval ({why}) but no one can approve it here. Find a safer way."
            if not self.ask(f"Allow command ({why})?\n\n{command}"):
                return "Error: the user denied this command. Do not retry it; ask or choose another approach."
        limit = timeout or self.cfg.bash_timeout
        p = subprocess.Popen(
            command, shell=True, cwd=self.cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
            start_new_session=True,  # own process group, so we can kill grandchildren too
        )
        deadline, reason = time.monotonic() + limit, ""
        while True:
            try:
                out, err = p.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if self.stop.is_set():
                    reason = "Error: interrupted by the user"
                elif time.monotonic() > deadline:
                    reason = f"Error: timed out after {limit}s"
                if reason:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    out, err = p.communicate()
                    break
        text = (out or "") + (("\n[stderr]\n" + err) if err else "")
        if reason:
            return f"{reason}\n{clip(text.strip(), 3000)}"
        return f"{text.strip() or '(no output)'}\n[exit {p.returncode}]"

    def read_file(self, path: str, start: int = 1, end: int = 400) -> str:
        p = self.path(path)
        if not p.is_file():
            return f"Error: not a file: {p}"
        lines = p.read_text(errors="replace").splitlines()
        self.read_files.add(str(p))
        s, e = max(1, start), min(len(lines), end)
        body = "\n".join(f"{i + 1}\t{lines[i]}" for i in range(s - 1, e))
        more = f"\n[showing {s}-{e} of {len(lines)} lines]" if e < len(lines) or s > 1 else ""
        return body + more

    def write_file(self, path: str, content: str) -> str:
        p = self.path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.read_files.add(str(p))
        return f"Wrote {len(content)} chars to {p}"

    def edit_file(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        p = self.path(path)
        if not p.is_file():
            return f"Error: not a file: {p}"
        text = p.read_text()
        n = text.count(old)
        if n == 0:
            return "Error: `old` not found. Use read_file to copy the exact text (whitespace matters)."
        if n > 1 and not replace_all:
            return f"Error: `old` matches {n} places. Add surrounding context or set replace_all."
        p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        return f"Edited {p} ({n if replace_all else 1} replacement)"

    def grep(self, pattern: str, path: str = ".", glob: str = "") -> str:
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "-n", "--no-heading", "--max-columns", "200", "-m", "50", pattern, str(self.path(path))]
            if glob:
                cmd[1:1] = ["-g", glob]
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=self.cwd)
            return r.stdout.strip() or "(no matches)"
        rx, hits = re.compile(pattern), []
        for f in self._walk(self.path(path)):
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f}:{i}:{line[:200]}")
                        if len(hits) >= 50:
                            return "\n".join(hits)
            except OSError:
                pass
        return "\n".join(hits) or "(no matches)"

    def find_files(self, pattern: str, path: str = ".") -> str:
        res = [str(f.relative_to(self.cwd)) if f.is_relative_to(self.cwd) else str(f)
               for f in self._walk(self.path(path)) if fnmatch.fnmatch(f.name, pattern) or fnmatch.fnmatch(str(f), pattern)]
        return "\n".join(res[:200]) or "(no files)"

    @staticmethod
    def _walk(root: Path):
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}
        if root.is_file():
            yield root
            return
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in skip]
            for fn in fns:
                yield Path(dp) / fn

    def image_label(self, path: str) -> str:
        """Stable name for an image, used to label it in context (URL or absolute path)."""
        return path if path.startswith(("http://", "https://")) else str(self.path(path))

    def view_image(self, path: str) -> str:
        from .vision import load_image_part

        label = self.image_label(path)
        if path.startswith(("http://", "https://")):
            path = web.fetch_to_temp(path)
        p = self.path(path)
        if not p.is_file():
            return f"Error: not a file: {p}"
        part, note = load_image_part(str(p), self.cfg)
        # Each image is preceded by its own label so the model knows which file it is
        # looking at, even when several are viewed in one step.
        self.pending_images += [{"type": "text", "text": f"[image: {label}]"}, part]
        return f"Image attached below as [image: {label}] ({note.split(' (', 1)[-1].rstrip(')')})"

    def inspect_image(self, path: str) -> str:
        """Inspect image dimensions, format, and aspect ratio without loading it as visual tokens."""
        p = self.path(path)
        if not p.is_file():
            return f"Error: not a file: {p}"
        try:
            from PIL import Image
            with Image.open(p) as img:
                w, h = img.size
                ratio = w / h if h else 0
                return f"{p.name}: {img.format} {w}x{h} (ratio {ratio:.2f}, {'16:9' if abs(ratio - 16/9) < 0.05 else 'not 16:9'})"
        except Exception as e:
            return f"Error reading image: {e}"

    def set_wallpaper(self, path: str) -> str:
        """Copy the image into the wallpaper folder (if it isn't there yet) and apply it."""
        src = self.path(path)
        if not src.is_file():
            return f"Error: not a file: {src}"
        wdir = Path(os.path.expanduser(self.cfg.wallpaper_dir))
        wdir.mkdir(parents=True, exist_ok=True)
        dest = src if src.resolve().parent == wdir.resolve() else wdir / src.name
        if dest != src:
            if dest.exists() and dest.read_bytes() != src.read_bytes():
                dest = wdir / f"{src.stem}-{int(time.time())}{src.suffix}"
            shutil.copy2(src, dest)
        cmds = [self.cfg.wallpaper_cmd] if self.cfg.wallpaper_cmd else _wallpaper_setters()
        if not cmds:
            return (f"Error: saved to {dest}, but no wallpaper setter was found. Find how this desktop sets "
                    f"wallpapers (its config/CLI), then set QH_WALLPAPER_CMD (e.g. 'tool {{path}}') via propose_note.")
        errors = []
        for tpl in cmds:
            cmd = tpl.replace("{path}", shlex.quote(str(dest)))
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=self.cwd)
            if r.returncode == 0:
                return f"Wallpaper set: {dest} (via: {cmd})"
            errors.append(f"{cmd} -> exit {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}")
        return f"Error: saved to {dest}, but setting it failed:\n" + "\n".join(errors)

    # ------------------------------------------------------------- definitions
    def _build(self) -> list[Tool]:
        return [
            Tool("bash", "Run a shell command in the project dir. Returns stdout/stderr and exit code.",
                 _obj({"command": S, "timeout": I}, ["command"]), self.bash),
            Tool("read_file", "Read a text file with line numbers. Default lines 1-400; use start/end for more.",
                 _obj({"path": S, "start": I, "end": I}, ["path"]), self.read_file, True),
            Tool("write_file", "Create or overwrite a file with the full content.",
                 _obj({"path": S, "content": S}, ["path", "content"]), self.write_file),
            Tool("edit_file", "Replace exact text `old` with `new` in a file. Prefer this over write_file for changes.",
                 _obj({"path": S, "old": S, "new": S, "replace_all": {"type": "boolean"}}, ["path", "old", "new"]), self.edit_file),
            Tool("grep", "Regex search file contents (max 50 hits). Optional glob like '*.py'.",
                 _obj({"pattern": S, "path": S, "glob": S}, ["pattern"]), self.grep, True),
            Tool("find_files", "Find files by name glob, e.g. '*.py'.",
                 _obj({"pattern": S, "path": S}, ["pattern"]), self.find_files, True),
            Tool("load_skill", "Load a skill playbook by name (see the skill list in the system prompt).",
                 _obj({"name": S}, ["name"]), lambda name: knowledge.load_skill(self.skills, name), True),
            Tool("propose_note", "Propose a durable lesson/fact worth remembering for future sessions (user approves). Use sparingly, only for non-obvious, reusable facts.",
                 _obj({"note": S}, ["note"]), lambda note: knowledge.propose_note(note, self.ask)),
            Tool("web_search", "Search the web. Returns title, URL, snippet for ~8 results.",
                 _obj({"query": S}, ["query"]), lambda query: web.web_search(query), True),
            Tool("fetch_url", "GET a URL and return text (HTML is stripped; JSON as-is). For APIs and pages.",
                 _obj({"url": S, "max_chars": I}, ["url"]), lambda url, max_chars=8000: web.fetch_url(url, max_chars), True),
            Tool("download", "Download a URL to a local path. Reports size and image resolution.",
                 _obj({"url": S, "path": S}, ["url", "path"]), lambda url, path: web.download(url, str(self.path(path)))),
            Tool("inspect_image", "Fast image metadata check (dimensions, format, aspect ratio) without spending visual tokens. Also aliased as 'identify'.",
                 _obj({"path": S}, ["path"]), self.inspect_image, True),
            Tool("identify", "Alias for inspect_image: get image dimensions and format quickly.",
                 _obj({"path": S}, ["path"]), self.inspect_image, True),
            Tool("view_image", "Look at an image (local path or http URL; prefer small thumbnail URLs). Shown to you in the next message.",
                 _obj({"path": S}, ["path"]), self.view_image),
            Tool("set_wallpaper", "Apply an image (local path, e.g. just downloaded) as the desktop wallpaper. "
                 "Copies it into the user's wallpaper folder first.",
                 _obj({"path": S}, ["path"]), self.set_wallpaper),
        ]
